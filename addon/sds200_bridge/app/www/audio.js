/* Live scanner audio in this page.
 *
 * Decoding is the strategy the Lovelace card arrived at after a long run of
 * real-install failures (see docs/protocol-notes.md, and the long comment
 * above `_startAudio` in custom_components/sds200/www/sds200-card.js):
 * fragmented MP4/AAC fed to a MediaSource on a real <audio> element.
 * Reimplemented here rather than shared -- the card ships inside the Home
 * Assistant integration and this ships inside the add-on image, two
 * separately-deployed artifacts with no file either can import from the
 * other.
 *
 * How the bytes get here is where this page differs from the card, and it
 * is the whole reason this file exists.
 *
 * Why it isn't just `<audio src="scanners/x/audio/stream.mp3">`, or a plain
 * fetch(): this page is served through ingress, i.e. from Home Assistant's
 * own origin, where HA's frontend service worker intercepts every
 * same-origin request (`/(api|auth)/.*` -> NetworkOnly, and a catch-all
 * behind it -- either way the request passes through the worker). Its
 * handling of a never-ending response body gives out after ~30 seconds:
 * confirmed symptom, "TypeError: Error in input stream" at 30.0s, on
 * desktop Firefox and in the HA Android app's WebView.
 *
 * The card escapes that by running its fetch inside an
 * `<iframe sandbox="allow-scripts">`, whose opaque origin has no service
 * worker registration. That trick CANNOT work behind ingress, which is what
 * this page got wrong until 2026-08-05: an opaque origin sends no cookies
 * and is cross-origin to everything, while ingress authenticates by the
 * `ingress_session` cookie and sends no CORS headers -- so the relay's
 * request was refused twice over, leaving only the in-page fetch it was
 * there to replace, and with it the ~30s cutoff.
 *
 * What works here is a WebSocket. Service workers never see the handshake
 * (there is no fetch event for one), it is an ordinary same-origin request
 * so the ingress cookie goes with it, and ingress proxies WebSockets
 * natively -- all three already proven in this deployment by the status
 * socket this same page keeps open. The in-page fetch stays as the fallback
 * for anything that can't get a socket up (and outside Supervisor, where
 * there is no ingress and no service worker, it works fine).
 *
 * The iframe-navigation fallback the card also carries is deliberately not
 * here -- it relies on the browser rendering a media document for
 * `audio/mp4`, which the HA Android app's WebView does not do, and this
 * page has a real <audio> element either way.
 */

"use strict";

(function (SDS) {
  // Must match what audio_bridge.py's ffmpeg actually emits (fragmented
  // MP4/AAC-LC). Checked with isTypeSupported() before use, since a
  // wrong/unsupported string makes addSourceBuffer() throw.
  const MIME = 'audio/mp4; codecs="mp4a.40.2"';
  // Backstop for a stream that connects but never produces decodable audio.
  // The known relay failure rejects its fetch well inside this.
  const START_TIMEOUT_MS = 6000;
  // This stream runs for hours, and a SourceBuffer that is only ever
  // appended to eventually hits QuotaExceededError -- much sooner on mobile,
  // where the buffer budget is far smaller. Cheap to keep two minutes: mono
  // 8 kHz AAC is a fraction of a megabyte.
  const BUFFER_KEEP_S = 120;

  /** One scanner's playback. Create per Play click, discard on Stop -- a
   *  session object rather than instance state so a stale async callback
   *  from a previous session can tell that it is stale. */
  class AudioPlayer {
    constructor(scannerId, { onStatus, onStopped } = {}) {
      this.scannerId = scannerId;
      this.onStatus = onStatus || function () {};
      // Called when playback ends on its own, i.e. not from a Stop click --
      // the caller's button is still saying "Stop" at that point and has no
      // other way to find out (start() resolved long ago).
      this.onStopped = onStopped || function () {};
      this.session = null;
      this.sink = document.getElementById("audio-sink");
    }

    get playing() {
      return this.session !== null;
    }

    async start() {
      this.stop();
      const session = { stopped: false, startedAt: Date.now(), wakeLock: null };
      this.session = session;
      this._status("connecting…");
      this._keepAwake(session);

      for (const transport of ["ws", "direct"]) {
        const started = await this._play(session, transport);
        if (session.stopped || this.session !== session) return;
        if (started) return;
        this._teardown(session);
      }
      this._status("could not start audio — check the add-on log");
      this.stop();
    }

    stop() {
      const session = this.session;
      this.session = null;
      if (!session) return;
      session.stopped = true;
      if (session.wakeLock) {
        session.wakeLock.release().catch(() => {});
        session.wakeLock = null;
      }
      this._teardown(session);
      this._status("");
    }

    _status(text) {
      this.onStatus(text);
    }

    /* Listening to a scanner is exactly the case where nobody touches the
       screen for minutes at a time, so hold it awake while audio plays. The
       card carries a <video> fallback for platforms that refuse the wake
       lock; here the lock is best-effort only -- this page is far more
       likely to be open on a desktop, and a silently-playing hidden video
       is a lot of machinery for the difference. */
    async _keepAwake(session) {
      if (!navigator.wakeLock) return;
      try {
        const sentinel = await navigator.wakeLock.request("screen");
        if (session.stopped) {
          sentinel.release().catch(() => {});
          return;
        }
        session.wakeLock = sentinel;
      } catch (err) {
        /* Refused (not user-activated, or unsupported) -- audio still
           plays; the screen may just sleep. */
      }
    }

    /** This scanner's audio endpoints. Relative, like every other URL this
     *  page builds -- see util.js for why. */
    _url(transport) {
      const base = `scanners/${encodeURIComponent(this.scannerId)}/audio/`;
      return transport === "ws" ? SDS.wsUrl(`${base}ws`) : `${base}stream.mp3`;
    }

    _play(session, transport) {
      return new Promise((resolve) => {
        if (!window.MediaSource || !window.MediaSource.isTypeSupported(MIME)) {
          console.warn("sds200: MediaSource can't play", MIME);
          resolve(false);
          return;
        }

        let settled = false;
        let playing = false;
        const finish = (ok) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(ok);
        };
        const timer = setTimeout(() => {
          if (playing) return;
          console.warn("sds200: no audio within %dms via %s", START_TIMEOUT_MS, transport);
          finish(false);
        }, START_TIMEOUT_MS);
        /* The byte source running out. Before playback started that just
           means this transport didn't work and the next one gets a turn
           (finish(false)); after it, there is no next one -- the session is
           over, so put the button back to Listen and say why rather than
           leave it on Stop over an audio element nothing is feeding. */
        const ended = (why) => {
          if (playing && this.session === session) {
            this.stop();
            this._status(why);
            this.onStopped();
          }
          finish(playing);
        };

        const audio = document.createElement("audio");
        audio.autoplay = true;
        // Mobile WebKit/WebView can otherwise insist on taking playback
        // fullscreen -- meaningless for audio, and it would hijack the page.
        audio.playsInline = true;
        audio.addEventListener("playing", () => {
          playing = true;
          this._status(`playing (${transport})`);
          finish(true);
        });
        audio.addEventListener("error", () => ended("the browser could not decode the audio"));
        // In the DOM (in the zero-size sink) rather than detached: a
        // detached media element is eligible for garbage collection while
        // it is playing.
        this.sink.appendChild(audio);
        session.audio = audio;

        const url = this._url(transport);
        const source =
          transport === "ws" ? this._wsSource(session, url) : this._directSource(session, url);
        session.source = source;

        const mediaSource = new MediaSource();
        session.mediaSource = mediaSource;
        mediaSource.addEventListener(
          "sourceopen",
          () => {
            URL.revokeObjectURL(audio.src);
            let buffer;
            try {
              buffer = mediaSource.addSourceBuffer(MIME);
            } catch (err) {
              console.warn("sds200: addSourceBuffer failed", err);
              finish(false);
              return;
            }
            this._pump(session, source, buffer).then(
              // A clean end after playback started is the add-on dropping
              // this subscriber -- its audio session restarted, or we fell
              // too far behind to be sent a coherent stream (see
              // audio_bridge.py's STREAM_CLOSED).
              () => ended("the add-on ended the stream — press Listen to reconnect"),
              (err) => {
                console.warn("sds200: %s stream failed", transport, err);
                ended("the audio stream failed — check the add-on log");
              }
            );
          },
          { once: true }
        );
        audio.src = URL.createObjectURL(mediaSource);
        audio.play().catch(() => {
          /* Autoplay refusal -- this always runs from a click, so this is
             the browser being conservative rather than a real failure. The
             "playing" listener above is what actually decides. */
        });
      });
    }

    /** Bytes over a WebSocket -- the transport that gets past HA's service
     *  worker while still being an ordinary authenticated same-origin
     *  request (see the module comment). Binary frames, each one an ffmpeg
     *  chunk, starting with the init segment the add-on replays to every
     *  new subscriber. */
    _wsSource(session, url) {
      let socket;
      try {
        socket = new WebSocket(url);
      } catch (err) {
        // Constructor throws only on a malformed URL/scheme; a connection
        // that just fails arrives as an "error" event instead.
        return {
          next: () => Promise.reject(err),
          close: () => {},
        };
      }
      socket.binaryType = "arraybuffer";

      const queue = [];
      let waiting = null;
      let ended = null;
      const wake = () => {
        if (!waiting) return;
        const w = waiting;
        if (queue.length) {
          waiting = null;
          w.resolve({ done: false, value: queue.shift() });
        } else if (ended) {
          waiting = null;
          if (ended.error) w.reject(ended.error);
          else w.resolve({ done: true });
        }
      };
      socket.addEventListener("message", (event) => {
        // Text frames would be a protocol change on the add-on's side, not
        // something to guess at the meaning of here.
        if (typeof event.data === "string") return;
        queue.push(event.data);
        wake();
      });
      // A socket that closes before any bytes arrive is a failed transport
      // (ingress refused it, no route to the add-on); one that closes after
      // them is the add-on ending the stream. _pump can't tell the
      // difference and doesn't need to -- both mean "no more bytes", and
      // whether playback had started is what decides what happens next.
      socket.addEventListener("close", () => {
        ended = ended || {};
        wake();
      });
      socket.addEventListener("error", () => {
        ended = ended || { error: new Error("the audio websocket failed") };
        wake();
      });

      return {
        next: () =>
          new Promise((resolve, reject) => {
            if (queue.length) resolve({ done: false, value: queue.shift() });
            else if (ended && ended.error) reject(ended.error);
            else if (ended) resolve({ done: true });
            else waiting = { resolve, reject };
          }),
        close: () => {
          socket.close();
          // Release a pending next(): "close" fires asynchronously, and
          // _pump would await it until then for no reason.
          ended = ended || {};
          wake();
        },
      };
    }

    /** Bytes fetched in this document -- i.e. through the service worker,
     *  with everything that implies (see the module comment). The fallback,
     *  not the choice: it is known to stop at ~30s behind ingress, but it
     *  needs nothing but a plain HTTP GET, and outside Supervisor -- the
     *  add-on run directly on :8000, no ingress and no service worker in
     *  front of it -- there is nothing wrong with it at all. */
    _directSource(session, url) {
      const controller = new AbortController();
      let reader = null;
      const connected = fetch(url, { signal: controller.signal, cache: "no-store" }).then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        if (!r.body) throw new Error("no response body to read");
        reader = r.body.getReader();
        return reader;
      });
      return {
        next: async () => (await connected).read(),
        close: () => {
          controller.abort();
          if (reader) reader.cancel().catch(() => {});
        },
      };
    }

    async _pump(session, source, buffer) {
      const audio = session.audio;
      const append = (chunk) =>
        new Promise((resolve, reject) => {
          const run = () => {
            buffer.addEventListener("updateend", () => resolve(), { once: true });
            buffer.addEventListener(
              "error",
              () => reject(new Error("SourceBuffer error")),
              { once: true }
            );
            try {
              buffer.appendBuffer(chunk);
            } catch (err) {
              reject(err);
            }
          };
          // A SourceBuffer rejects appendBuffer() while it is updating, and
          // _trim's remove() below leaves it updating -- so wait it out
          // rather than dropping the chunk on the floor.
          if (buffer.updating) buffer.addEventListener("updateend", run, { once: true });
          else run();
        });

      while (true) {
        // session.stopped, or this session having been abandoned for the
        // next transport: either way the MediaSource is gone or going, so
        // stop touching the SourceBuffer.
        if (session.stopped || this.session !== session) return;
        const { done, value } = await source.next();
        if (done) return;
        if (session.stopped || this.session !== session) return;
        this._trim(audio, buffer);
        try {
          await append(value);
        } catch (err) {
          if (err && err.name === "QuotaExceededError") {
            // Evict everything already played and try once more, rather
            // than dropping the stream over a full buffer.
            if (buffer.buffered.length) {
              const end = Math.max(buffer.buffered.start(0), (audio.currentTime || 0) - 5);
              if (end > buffer.buffered.start(0)) buffer.remove(buffer.buffered.start(0), end);
            }
            await append(value);
          } else {
            throw err;
          }
        }
      }
    }

    /** Drop decoded audio well behind the playhead, and re-seek if the
     *  element has drifted out of the buffered range (which is what a
     *  network stall looks like from here). */
    _trim(audio, buffer) {
      const buffered = buffer.buffered;
      if (!buffered.length || buffer.updating) return;
      const start = buffered.start(0);
      const end = buffered.end(buffered.length - 1);
      if (audio.currentTime < start || audio.currentTime > end + 1) {
        try {
          audio.currentTime = end;
        } catch (err) {
          /* Not seekable yet; the next chunk will make it so. */
        }
      }
      const cutoff = Math.min(audio.currentTime, end) - BUFFER_KEEP_S;
      if (cutoff > start) buffer.remove(start, cutoff);
    }

    _teardown(session) {
      if (session.source) {
        session.source.close();
        session.source = null;
      }
      if (session.audio) {
        // Detach the MediaSource before dropping the element, so the
        // browser isn't left decoding into an element nothing references.
        session.audio.pause();
        session.audio.removeAttribute("src");
        session.audio.load();
        session.audio.remove();
        session.audio = null;
      }
      session.mediaSource = null;
    }
  }

  SDS.AudioPlayer = AudioPlayer;
})(window.SDS);
