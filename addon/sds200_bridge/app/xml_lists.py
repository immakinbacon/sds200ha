"""Reassembly for XML responses (GLT / GSI / PSI / MSI).

Confirmed against a real SDS200 (firmware 1.23.15), including a genuine
40+ packet GLT,SYS response (359 systems): **every** packet -- final or
not -- is a self-contained, well-formed document with its own
"<?xml ...?><ROOT>...</ROOT>" and, for GLT, its own embedded
"<Footer No="n" EOT="0|1"/>" just before the closing tag. There's no
"headerless continuation" shape at all; `Footer/@EOT` is the only thing
that tells you whether more packets are coming. (An earlier version of this
module assumed multi-packet responses omitted the closing tag until
EOT=1 -- wrong; don't reintroduce that assumption.)

GSI/PSI responses are always a single packet and never have a Footer at
all. Line separators inside the XML are bare "\\r", not "\\n"/"\\r\\n".

Reassembly handles this uniformly: strip the prolog/opening-tag from every
packet, remove the footer wherever it appears if present, then separately
strip a trailing closing tag if the body still has one -- "has a footer"
and "is a complete document" are independent conditions, not mutually
exclusive, and both need to be checked on every packet.
"""

from __future__ import annotations

import asyncio
import logging
import re
import xml.etree.ElementTree as ET

logger = logging.getLogger(__name__)

_XML_MARKER = "<XML>,"
_PROLOG_RE = re.compile(r"<\?xml[^>]*\?>\s*")
# Group 1: bare tag name (used for the synthesized closing tag / "is this
# already a complete document" check). Group 2: the tag's own attributes,
# if any, *including* the leading space -- e.g. ' Mode="Scan Mode"
# V_Screen="conventional_scan"', or "" for GLT's bare "<GLT>". Both are
# needed: root_tag alone silently dropped GSI/PSI's Mode/V_Screen attributes
# when reconstructing the wrapper (see module docstring / protocol-notes.md).
_ROOT_OPEN_RE = re.compile(r"<([A-Za-z_][\w.-]*)((?:\s[^>]*)?)>\s*")
_FOOTER_RE = re.compile(r'<Footer\s+No="(\d+)"\s+EOT="(\d)"\s*/>\s*')


async def reassemble(
    queue: "asyncio.Queue[bytes]", *, timeout: float = 2.0, expected_prefix: str | None = None
) -> ET.Element:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    root_tag: str | None = None
    root_open_tag: str | None = None
    inner_parts: list[str] = []
    expected_no = 1

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError("timed out reassembling XML response")
        data = await asyncio.wait_for(queue.get(), remaining)
        text = data.decode("utf-8", errors="replace")

        if expected_prefix and not text.startswith(expected_prefix):
            # Interleaved unsolicited/late packet (e.g. a stray GSI push) --
            # only filter on the *first* packet; once we've locked onto a
            # matching sequence, trust its own continuation packets.
            if not inner_parts:
                logger.debug("ignoring unmatched XML datagram %r while waiting for %r", text[:60], expected_prefix)
                continue

        idx = text.find(_XML_MARKER)
        body = text[idx + len(_XML_MARKER):] if idx != -1 else text
        # Real responses leave a stray "\r" between the "<XML>," marker and
        # "<?xml ...?>" that _PROLOG_RE's trailing \s* doesn't reach (it's
        # *before* the match, not after) -- strip leading whitespace first.
        body = body.lstrip()
        body = _PROLOG_RE.sub("", body, count=1).lstrip()

        m = _ROOT_OPEN_RE.match(body)
        if m:
            if root_tag is None:
                root_tag = m.group(1)
                root_open_tag = f"<{m.group(1)}{m.group(2)}>"
            body = body[m.end():]

        footer_m = _FOOTER_RE.search(body)
        eot = None
        if footer_m:
            no = int(footer_m.group(1))
            eot = footer_m.group(2)
            if no != expected_no:
                logger.warning("XML reassembly gap: expected packet %d, got %d", expected_no, no)
            expected_no = no + 1
            # Remove the footer wherever it sits -- it can be mid-body
            # (single-packet GLT-style, closing tag still to come) or
            # trailing (multi-packet GLT-style, no closing tag at all).
            body = body[: footer_m.start()] + body[footer_m.end():]

        # Independent of whether a footer was present: if what's left is
        # already a complete document (GSI/PSI-style, or single-packet
        # GLT-style after footer removal), strip its closing tag so we
        # don't double it when synthesizing the wrapper below.
        if root_tag and body.rstrip().endswith(f"</{root_tag}>"):
            body = body.rstrip()[: -len(f"</{root_tag}>")]

        inner_parts.append(body)
        if eot == "1" or footer_m is None:
            break

    if root_tag is None or root_open_tag is None:
        raise ValueError("could not determine XML root tag from response")
    xml_text = f"{root_open_tag}{''.join(inner_parts)}</{root_tag}>"
    return ET.fromstring(xml_text)


def element_to_dicts(root: ET.Element) -> list[dict]:
    """Flatten a reassembled list-style root (e.g. GLT) into a list of dicts.

    reassemble() should already have stripped every "Footer" marker, but
    skip any that slip through anyway -- it's protocol pagination metadata,
    never actual list data.
    """
    result = []
    for child in root:
        if child.tag == "Footer":
            continue
        entry = dict(child.attrib)
        entry["_tag"] = child.tag
        result.append(entry)
    return result


def gsi_to_dict(root: ET.Element) -> dict:
    """Flatten a GSI/PSI <ScannerInfo> response into a plain dict.

    Which children are present depends on Mode (see the "Depend on mode
    elements" matrix in the spec / docs/protocol-notes.md) -- this doesn't
    hardcode that, it just carries over whatever attributes showed up.
    """
    result: dict = {"mode": root.attrib.get("Mode"), "v_screen": root.attrib.get("V_Screen")}
    for child in root:
        if child.tag == "ViewDescription":
            result["view_description"] = {sub.tag: _view_element(sub) for sub in child}
        else:
            result[child.tag] = dict(child.attrib)
    return result


def _view_element(element: ET.Element) -> dict:
    """One ViewDescription child, keeping any children it has of its own.

    Everything else in GSI is one flat level of attributes, and this was too
    until a real weather alert turned up a `<PopupScreen>` with a `<Button
    Text='"E" (OK)' KeyCode="E" />` inside it. That KeyCode is the only
    machine-readable way off a modal popup -- the popup replaces the
    soft-key row entirely, so STS has no labels to read -- and dropping the
    children threw it away. Buttons land under "buttons" alongside the
    element's own attributes, so `view_description["OverWrite"]["Text"]` and
    every other existing reader is untouched.
    """
    result = dict(element.attrib)
    buttons = [dict(button.attrib) for button in element if button.tag == "Button"]
    if buttons:
        result["buttons"] = buttons
    return result
