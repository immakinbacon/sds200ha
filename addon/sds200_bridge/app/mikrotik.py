"""MikroTik RouterOS REST API client for PoE-power-cycling a switch port.

The SDS200 has no documented network reboot command of its own (see
docs/protocol-notes.md), and its RTSP/audio server is known to wedge with
power-cycling as the only recovery seen so far. For a scanner that's PoE-
powered from a MikroTik switch, cutting and restoring power at the switch
port is the practical network-triggerable equivalent of unplugging it.

RouterOS exposes this as a single CLI action, `/interface ethernet poe
power-cycle <interface>` (which itself briefly disables then re-enables PoE
detection on the port -- not something this client implements by hand as
two separate off/on calls), and the REST API maps CLI actions to a POST at
the same menu path with the action name appended, body = the command's
named arguments as JSON.

Real-install finding: the request body's key for identifying the target
interface is `numbers` (accepts the interface name, e.g. `"ether12"`), NOT
`interface` -- RouterOS's REST API rejected `{"interface": ...}` outright
with `{"detail": "unknown parameter interface", "error": 400, ...}`. Not
documented anywhere obvious; found by testing directly against a real
router after repeated silent failures.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)


class MikrotikApiError(Exception):
    pass


class MikrotikPoeReset:
    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        interface: str,
        verify_ssl: bool = True,
        use_ssl: bool = True,
    ):
        self.host = host
        self.username = username
        self.password = password
        self.interface = interface
        self.verify_ssl = verify_ssl
        # RouterOS's REST API is served by the "www"/"www-ssl" services,
        # configured independently of each other -- a router with only
        # "www" (plain HTTP) enabled and "www-ssl" disabled/not set up just
        # silently fails to connect on https://, with nothing to distinguish
        # that from a wrong password/interface. Real-install finding: this
        # was exactly why power-cycle calls weren't taking effect.
        self.use_ssl = use_ssl

    @property
    def url(self) -> str:
        """The full REST endpoint, exposed so callers can log *which* scheme
        and host a power-cycle actually went to. Logging only the interface
        name (what trigger_reboot() used to do) left http-vs-https -- the
        single most likely misconfiguration here -- invisible in the add-on
        log, which is what made this take so long to pin down.
        """
        scheme = "https" if self.use_ssl else "http"
        return f"{scheme}://{self.host}/rest/interface/ethernet/poe/power-cycle"

    async def power_cycle(self) -> None:
        """POST the REST equivalent of `/interface ethernet poe power-cycle
        <interface>`. RouterOS's own power-cycle command already handles the
        off/on timing -- this is one call, not a manual off-then-on pair.
        """
        url = self.url
        auth = aiohttp.BasicAuth(self.username, self.password)
        # RouterOS's default REST cert is self-signed unless the user has
        # replaced it -- verify_ssl=False is expected to be the common case,
        # not a fallback for errors. Irrelevant (and harmless) when
        # use_ssl=False -- there's no TLS to verify either way.
        connector = None if (self.verify_ssl or not self.use_ssl) else aiohttp.TCPConnector(ssl=False)
        try:
            async with aiohttp.ClientSession(auth=auth, connector=connector) as session:
                async with session.post(
                    url,
                    json={"numbers": self.interface},
                    # sock_connect is deliberately much shorter than total:
                    # the one failure mode seen in practice is the TCP SYN
                    # being black-holed (nothing listening on the port and
                    # the router dropping rather than refusing), which with
                    # only a total= budget burns the entire 15s before
                    # saying anything. 5s is plenty for a switch on the LAN,
                    # and failing fast is what makes the hint below land
                    # while the user is still watching the log.
                    timeout=aiohttp.ClientTimeout(total=15, sock_connect=5),
                ) as resp:
                    if resp.status >= 400:
                        body = await resp.text()
                        raise MikrotikApiError(
                            f"MikroTik REST API error {resp.status} for interface "
                            f"{self.interface!r}: {body}"
                        )
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # asyncio.TimeoutError (raised when ClientTimeout.total expires)
            # is NOT an aiohttp.ClientError subclass -- a real-install crash
            # showed this escaping uncaught straight through trigger_reboot()
            # into the request handler (logged by aiohttp's own server-level
            # "Error handling request", not our own logger) whenever the
            # router genuinely didn't respond within the 15s budget, instead
            # of failing cleanly like every other error path here.
            # Real-install root cause, found by probing the router directly
            # from a LAN host: port 80 answered RouterOS's own 401 in 6ms
            # while port 443 silently dropped the SYN and hung until the
            # timeout expired -- i.e. "www" enabled, "www-ssl" not. Since
            # use_ssl defaults to true, the failure is a featureless hang
            # with nothing pointing at the scheme. Say so outright rather
            # than making the next person rediscover it.
            hint = ""
            if self.use_ssl:
                hint = (
                    " -- if this timed out on connect, check whether the router's "
                    "'www-ssl' service is actually enabled (RouterOS enables plain "
                    "'www' by default and leaves www-ssl off); if not, set "
                    "poe_reset_use_ssl: false"
                )
            raise MikrotikApiError(f"could not reach MikroTik REST API at {url}: {exc}{hint}") from exc
