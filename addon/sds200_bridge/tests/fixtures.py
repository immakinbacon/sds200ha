"""Raw UDP payloads captured from a real Uniden SDS200 (firmware 1.23.15,
2026-07-21) during development. Used as regression fixtures so the
reassembly/parsing bugs found and fixed during that session (see
docs/protocol-notes.md) can't silently come back.

Some entries are trimmed (fewer <SYS>/<FL> children than the real packet
had) for fixture brevity -- that's noted per-fixture below where it applies.
The shapes that matter for the reassembly logic (footer-before-closing-tag,
control chars, tab-escaping, etc.) are preserved verbatim.
"""

# STS response, single line captured mid-scan.
STS_RESPONSE = (
    b'STS,00001010100000000,              Jul21 21:02     ,'
    b'                              ,F0:0---------               ,'
    b'                              ,S0:----------   VOL: 4 SQL: 4 ,'
    b'                              ,D0:----------   Tag:--.--.--- ,'
    b'                              ,Coast Guard - USA             ,'
    b'                              ,Full Database                 ,'
    b'                              ,Nationwide UHF                ,'
    b'                              ,                              ,'
    b'                              ,Scanning...                   ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,Sys ID: ---     TGID: ---     ,'
    b'                              ,RFSS ID: ---    Site ID: ---  ,'
    b'                              ,WACN: ---       NOISE:38500   ,'
    b'                              ,UID: ---        RSSI: ---     ,'
    b'                              ,                           WX ,'
    b'                              , SYSTEM      DEPT     CHANNEL ,'
    b'********* ********** *********,0,1,0,0,,,0,OFF,3'
)

# STS response with scroll/blink control characters embedded in a channel
# name field mid-marquee-animation -- protocol._strip_control_chars exists
# because of this exact capture.
STS_RESPONSE_WITH_CONTROL_CHARS = (
    b'STS,00001010100000000,              Jul21 21:06     ,'
    b'                              ,F0:0---------               \x1a\x1b,'
    b'                              ,S0:----------   VOL: 4 SQL: 4 ,'
    b'                              ,D0:----------   Tag:--.--.--- ,'
    b'                              ,Indiana Military Operations   ,'
    b'                              ,Full Database                 ,'
    b'                              ,Grissom Air Reserve Base - 4\x06\x07,'
    b'                              ,                              ,'
    b'                              ,Scanning...                   ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,Sys ID: ---     TGID: ---     ,'
    b'                              ,RFSS ID: ---    Site ID: ---  ,'
    b'                              ,WACN: ---       NOISE:17172   ,'
    b'                              ,UID: ---        RSSI: ---     ,'
    b'                              ,                           WX ,'
    b'                              , SYSTEM      DEPT     CHANNEL ,'
    b'********* ********** *********,0,1,0,0,,,0,OFF,3'
)

# STS response containing custom LCD glyph bytes (0xAC 0xAD, likely status
# icons -- no documented mapping) trailing the clock line. Captured live
# during a real HA OS install session: the raw bytes were b'...22:46 \xac\xad'.
# Decoded with errors="replace" (as protocol.py's send_command does), these
# become U+FFFD -- protocol._strip_control_chars strips those too, not just
# the low control-byte range, because of this exact capture.
STS_RESPONSE_WITH_HIGH_BYTES = (
    b'STS,00001010100000000,              Jul21 22:46 \xac\xad  ,'
    b'                              ,F0:0---------               ,'
    b'                              ,S0:----------   VOL: 4 SQL: 4 ,'
    b'                              ,D0:----------   Tag:--.--.--- ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                              ,'
    b'                              ,                           WX ,'
    b'                              , SYSTEM      DEPT     CHANNEL ,'
    b'********* ********** *********,0,1,0,0,,,0,OFF,3'
)

# GST response. NOTE: the display-line portion is verbatim from a real
# capture, but the full trailing-fields tail was truncated in the terminal
# output when this was captured (only the first 160 chars were logged) --
# the tail below is reconstructed to match the documented GST_TRAILING_FIELDS
# order (protocol.py), not a byte-for-byte capture. Treat this fixture as
# "tests the parser's field-splitting logic," not "proves real trailing
# field values."
GST_RESPONSE = (
    b'GST,00001010100000000,              Jul21 21:03     ,,'
    b'F0:0---------               ,,'
    b'S0:----------   VOL: 4 SQL: 4 ,,'
    b'D0:----------   Tag:--.--.--- ,,'
    b'Indiana Military Operations   ,,'
    b'Full Database                 ,,'
    b'Military Operational Areas (,,'
    b',,'
    b'Scanning...                   ,,'
    b',,'
    b',,'
    b'Sys ID: ---     TGID: ---     ,,'
    b'RFSS ID: ---    Site ID: ---  ,,'
    b'WACN: ---       NOISE:30583   ,,'
    b'UID: ---        RSSI: ---     ,,'
    b'                           WX ,,'
    b' SYSTEM      DEPT     CHANNEL ,'
    b'*********,**********,*********,'
    b'1,OFF,3,,,,,,,,,'
)

# GSI response: single packet, complete well-formed <ScannerInfo> document,
# no Footer at all, "\r" line separators. Scan-mode capture.
GSI_RESPONSE_SCAN_MODE = (
    b'GSI,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r'
    b'<ScannerInfo Mode="Scan Mode" V_Screen="conventional_scan">\r'
    b'  <MonitorList Name="Full Database" Index="4294967295" ListType="FullDb" '
    b'Q_Key="None" N_Tag="None" DB_Counter="0" />\r'
    b'  <System Name="Family Radio Service (FRS) - USA" Index="20214" Avoid="Off" '
    b'SystemType="Conventional" Q_Key="None" N_Tag="None" Hold="Off" />\r'
    b'  <Department Name="Family Radio Service (FRS)" Index="20217" Avoid="Off" '
    b'Q_Key="None" Hold="Off" />\r'
    b'  <ConvFrequency Name="Channel 3" Index="20251" Avoid="Off" '
    b'Freq=" 462.612500MHz" Mod="NFM" N_Tag="None" Hold="Off" SvcType="Other" '
    b'P_Ch="Off" SAS="All" SAD="None" RecSlot="Slot None" LVL="0" IFX="Off" '
    b'TGID="TGID None" U_Id="UID None" />\r'
    b'  <DualWatch PRI="Off" CC="Off" WX="Priority" />\r'
    b'  <Property F="Off" VOL="4" SQL="4" Sig="0" Att="Off" Rec="Off" '
    b'KeyLock="Off" P25Status="None" Mute="Mute" Backlight="100" A_Led="Off" '
    b'Dir="Up" Rssi="-999" />\r'
    b'  <ViewDescription>\r'
    b'    <OverWrite Text="Scanning..." />\r'
    b'  </ViewDescription>\r'
    b'</ScannerInfo>\r'
)

# GLT,FL response: single packet, but *both* a complete document (closing
# </GLT>) *and* has an embedded <Footer .../> before that closing tag --
# distinct from the multi-packet GLT_SYS_PACKET_* shape below. This is the
# capture that exposed the end-anchored footer regex bug.
GLT_FL_RESPONSE = (
    b'GLT,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r<GLT>\r'
    b'  <FL Index="4294967295" Name="Full Database" Monitor="On" Q_Key="None" N_Tag="None" />\r'
    b'  <FL Index="4261412864" Name="Search with Scan" Monitor="Off" Q_Key="None" N_Tag="None" />\r'
    b'  <FL Index="0" Name="default" Monitor="On" Q_Key="0" N_Tag="None" />\r'
    b'  <Footer No="1" EOT="1"/>\r'
    b'</GLT>\r'
)

# GLT,SYS response: three consecutive real packets from a genuine 40+
# packet / 359-system exchange, all EOT="0". Confirms every packet --
# continuation or not -- is a self-closing complete document with the
# Footer embedded *before* the closing tag, not trailing with no closing
# tag as originally assumed.
GLT_SYS_PACKET_1 = (
    b'GLT,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r<GLT>\r'
    b'  <SYS Index="2" CountyId="693" Name="Adams" Avoid="Off" Type="Conventional" Q_Key="None" N_Tag="None" />\r'
    b'  <SYS Index="53" CountyId="694" Name="Allen" Avoid="Off" Type="Conventional" Q_Key="None" N_Tag="None" />\r'
    b'  <SYS Index="206" CountyId="695" Name="Bartholomew" Avoid="Off" Type="Conventional" Q_Key="None" N_Tag="None" />\r'
    b'  <Footer No="1" EOT="0"/>\r'
    b'</GLT>\r'
)
GLT_SYS_PACKET_2 = (
    b'GLT,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r<GLT>\r'
    b'  <SYS Index="575" CountyId="702" Name="Clark" Avoid="Off" Type="Conventional" Q_Key="None" N_Tag="None" />\r'
    b'  <SYS Index="666" CountyId="703" Name="Clay" Avoid="Off" Type="Conventional" Q_Key="None" N_Tag="None" />\r'
    b'  <Footer No="2" EOT="0"/>\r'
    b'</GLT>\r'
)
GLT_SYS_PACKET_3 = (
    b'GLT,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r<GLT>\r'
    b'  <SYS Index="1336" CountyId="711" Name="Dubois" Avoid="Off" Type="Conventional" Q_Key="None" N_Tag="None" />\r'
    b'  <Footer No="3" EOT="0"/>\r'
    b'</GLT>\r'
)

# A synthetic final packet (No="4", EOT="1") completing the sequence above.
# Not a raw capture (we only grabbed the first few packets of the real
# exchange) -- built by hand in the same confirmed shape so reassembly can
# be tested end-to-end through a clean EOT=1 termination.
GLT_SYS_PACKET_4_FINAL_SYNTHETIC = (
    b'GLT,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r<GLT>\r'
    b'  <SYS Index="9999" CountyId="999" Name="Last County" Avoid="Off" Type="Conventional" Q_Key="None" N_Tag="None" />\r'
    b'  <Footer No="4" EOT="1"/>\r'
    b'</GLT>\r'
)

# A stray unsolicited GSI push, of the kind observed landing in a
# GST/VOL/SQL request's response slot and causing desync before
# send_command()/send_xml_command() started matching by prefix.
STRAY_GSI_PUSH = GSI_RESPONSE_SCAN_MODE


# A real STS capture taken while the scanner was sitting in an actual weather
# alert (2026-08-11), which is the screen the add-on has to press its way off.
# Two things here are only visible on this screen and nowhere else in these
# fixtures:
#
# * the soft-key label row carries custom glyph bytes *between* the labels
#   ("to Scan", a run of \x01, "RESUME"), so a parser that strips them before
#   cutting labels out by column reports RESUME as the middle key.
# * DSP_FORM is 17 digits against 21 line pairs, a third data point for the
#   count being unreliable (the others are 17/18 and 17/17).
STS_RESPONSE_WEATHER_ALERT = (
    b'STS,00001110000000000,'
    b'              Aug11 10:28 \xac\xad  ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                VOL: 4 SQL: 4 ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'Weather Alert                 ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'CH 1   162.550000MHz          ,'
    b'******************************,'
    b'                \x0e\x0f\x0c       \x9c\x9d\x9e\x9f,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'Alert Only                    ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                NOISE:385     ,'
    b'                              ,'
    b'                RSSI: -49dBm  ,'
    b'                              ,'
    b'                \x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01  ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b' to Scan  \x01\x01\x01\x01\x01\x01\x01\x01\x01\x01  RESUME  ,'
    b'********* ********** *********,'
    b'1,1,0,0,,,5,OFF,3\r'
)

# The GSI from the same live alert. Confirms against hardware what the spec
# only promised: WxMode/@Mode really does read "Weather Alert" (SAME
# "Alert Only"), and the scanner names the screen itself in
# ScannerInfo/@V_Screen -- "wx_alert" here, "conventional_scan" once it is
# scanning again. Mode is "WX Hold": parked, not scanning.
GSI_RESPONSE_WEATHER_ALERT = (
    b'GSI,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r'
    b'<ScannerInfo Mode="WX Hold" V_Screen="wx_alert">\r'
    b'  <WxMode Mode="Weather Alert" SAME="Alert Only" />\r'
    b'  <WxChannel Index="0" CH_No="1" Freq=" 162.550000MHz" Mod="FM" Hold="On"'
    b' LVL="0" IFX="Off" />\r'
    b'  <Property F="Off" VOL="4" SQL="4" Sig="5" Att="Off" Rec="Off" KeyLock="Off"'
    b' P25Status="None" Mute="Mute" Backlight="100" A_Led="Off" Dir="Up" Rssi="-49" />\r'
    b'  <ViewDescription>\r  </ViewDescription>\r</ScannerInfo>\r'
)

# The *other* weather screen, captured live 2026-08-11 -- the one the alert
# actually opens with, and the one nothing could rescue.
#
# It is the same Mode/V_Screen as the pair above ("WX Hold"/"wx_alert"), so
# wx_parked is true and weather.py tries to get out. But the screen is a
# modal popup, not the soft-key screen: STS carries 10 line pairs and *no
# soft-key mask row at all*, so parse_sts returns soft_keys == [] and
# find_scan_key has nothing to find. With the fallback key unset (the
# default) the add-on logs "no soft key offering a way back" and gives up --
# a scanner parked until somebody walks over to it.
#
# GSI is where the way out lives: ViewDescription/PopupScreen names its own
# button. Note DSP_FORM is 10 digits here against the 17 every other capture
# has, a fourth data point for the count meaning nothing.
STS_RESPONSE_WEATHER_ALERT_POPUP = (
    b'STS,1111111111,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'Warning WX                    ,'
    b'                              ,'
    b'WX Alert                      ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'1,1,0,0,,,5,RED,3\r'
)

# The same popup in GSI. Two things only this capture has: the alert is live
# (WxMode "Weather Alert" with SAME, A_Led "Red", Mute "Mute"), and
# ViewDescription carries a PopupScreen whose Button element states the key
# that dismisses it. gsi_to_dict used to keep the PopupScreen's own
# attributes and drop its children, so the KeyCode -- the only machine-
# readable way out of this screen -- never reached the code that needed it.
GSI_RESPONSE_WEATHER_ALERT_POPUP = (
    b'GSI,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r'
    b'<ScannerInfo Mode="WX Hold" V_Screen="wx_alert">\r'
    b'  <WxMode Mode="Weather Alert" SAME="Alert Only" />\r'
    b'  <WxChannel Index="0" CH_No="1" Freq=" 162.550000MHz" Mod="FM" Hold="On"'
    b' LVL="0" IFX="Off" />\r'
    b'  <Property F="Off" VOL="4" SQL="4" Sig="5" Att="Off" Rec="Off" KeyLock="Off"'
    b' P25Status="None" Mute="Mute" Backlight="100" A_Led="Red" Dir="Up" Rssi="-49" />\r'
    b'  <ViewDescription>\r'
    b'    <PopupScreen Text="Warning WX&#xD;WX Alert        &#xD;&#xD;">\r'
    b'      <Button Text="&quot;E&quot; (OK)" KeyCode="E" />\r'
    b'    </PopupScreen>\r'
    b'  </ViewDescription>\r</ScannerInfo>\r'
)

# What the popup leaves behind, captured live immediately after dismissing it
# -- and the reason a single press is not a rescue. Still "WX Hold" on
# "wx_alert", still Hold="On" on WX channel 1: the scanner has not gone back
# to scanning, it has only stopped covering the screen. Now the soft-key row
# is here ("to Scan" / glyphs / "RESUME"), so the existing find_scan_key path
# takes over and a *second*, different press is what actually resumes.
#
# WxMode has also dropped to "Monitor Weather" with no SAME, so the alert
# state cleared while the screen stayed -- exactly the split wx_parked exists
# for. Driving the press off wx_alert would abandon the scanner right here.
STS_RESPONSE_WX_HOLD_AFTER_POPUP = (
    b'STS,00001110000000000,'
    b'              Aug11 13:19 \xac\xad  ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                VOL: 4 SQL: 4 ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'Monitor Weather               ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'CH 1   162.550000MHz          ,'
    b'******************************,'
    b'                \x0e\x0f\x0c       \x9c\x9d\x9e\x9f,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                NOISE:631     ,'
    b'                              ,'
    b'                RSSI: -49dBm  ,'
    b'                              ,'
    b'                \x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01\x01  ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b' to Scan  \x01\x01\x01\x01\x01\x01\x01\x01\x01\x01  RESUME  ,'
    b'********* ********** *********,'
    b'1,0,0,0,,,5,OFF,3\r'
)

GSI_RESPONSE_WX_HOLD_AFTER_POPUP = (
    b'GSI,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r'
    b'<ScannerInfo Mode="WX Hold" V_Screen="wx_alert">\r'
    b'  <WxMode Mode="Monitor Weather" />\r'
    b'  <WxChannel Index="0" CH_No="1" Freq=" 162.550000MHz" Mod="FM" Hold="On"'
    b' LVL="0" IFX="Off" />\r'
    b'  <Property F="Off" VOL="4" SQL="4" Sig="5" Att="Off" Rec="Off" KeyLock="Off"'
    b' P25Status="None" Mute="Unmute" Backlight="100" A_Led="Off" Dir="Up" Rssi="-49" />\r'
    b'  <ViewDescription>\r  </ViewDescription>\r</ScannerInfo>\r'
)

# A third weather screen, reached from the one above by pressing RESUME
# (soft3): Mode becomes "WX Scan" and the scanner walks the WX channels
# (CH 4, Hold="Off") -- still on V_Screen="wx_alert", so still not scanning
# the systems the user cares about, and wx_parked is right to call it parked.
# Its soft-key row proves the labels move: soft3 is now "HOLD" where it was
# "RESUME", while "to Scan" stays in the first column.
#
# NOTE: reconstructed, not raw. This was read back through a decode that
# replaced the two non-ASCII runs; they are restored here from the identical
# rows in the captures above (the date glyphs \xac\xad). Every byte the
# soft-key parsing touches is verbatim.
STS_RESPONSE_WX_SCAN = (
    b'STS,00001110000000000,'
    b'              Aug11 13:20 \xac\xad  ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                VOL: 4 SQL: 4 ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'Monitor Weather               ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'CH 4   162.425000MHz          ,'
    b'                              ,'
    b'                \x0e\x0f\x0c           ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b'                NOISE:2887    ,'
    b'                              ,'
    b'                RSSI:-101dBm  ,'
    b'                              ,'
    b'                \x01\x01\x01\x18\x18\x18\x18\x18\x18\x18\x18\x19  ,'
    b'                              ,'
    b'                              ,'
    b'                              ,'
    b' to Scan  \x01\x01\x01\x01\x01\x01\x01\x01\x01\x01   HOLD   ,'
    b'********* ********** *********,'
    b'1,0,0,0,,,3,OFF,3\r'
)

GSI_RESPONSE_WX_SCAN = (
    b'GSI,<XML>,\r<?xml version="1.0" encoding="utf-8"?>\r'
    b'<ScannerInfo Mode="WX Scan" V_Screen="wx_alert">\r'
    b'  <WxMode Mode="Monitor Weather" />\r'
    b'  <WxChannel Index="3" CH_No="4" Freq=" 162.425000MHz" Mod="FM" Hold="Off"'
    b' LVL="0" IFX="Off" />\r'
    b'  <Property F="Off" VOL="4" SQL="4" Sig="2" Att="Off" Rec="Off" KeyLock="Off"'
    b' P25Status="None" Mute="Unmute" Backlight="100" A_Led="Off" Dir="Up" Rssi="-107" />\r'
    b'  <ViewDescription>\r  </ViewDescription>\r</ScannerInfo>\r'
)
