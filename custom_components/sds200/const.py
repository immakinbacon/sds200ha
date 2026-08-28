DOMAIN = "sds200"

# HA add-ons are reachable from HA core on the internal "hassio" Docker
# network at http://<hostname>:<port> -- but the hostname is NOT reliably
# "<slug>" or "local_<slug>". Confirmed on a real HA OS install: a locally
# installed add-on (copied into /addons, not from a repository) got the
# hostname "aa65dcfd-sds200-bridge" (a Supervisor-generated hash prefix, not
# derived from the slug at all). There's no way to predict it in advance --
# users need to check their add-on's actual hostname (e.g. via its logs, or
# Settings > Add-ons > <name> > Info) and enter it in the config flow. This
# default is just a plausible starting guess for the *port*, not the host.
DEFAULT_HOST = ""
DEFAULT_PORT = 8000

MANUFACTURER = "Uniden"
MODEL = "SDS200"
