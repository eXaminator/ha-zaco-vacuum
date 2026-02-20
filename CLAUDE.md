# Zaaco A10 Reverse Engineering Project

## Goal
Reverse engineer the Zaaco A10 vacuum app API to build a Home Assistant integration.

## Project Structure
- `/apk_jadx_output` - Decompiled Java source from jadx
- `/apk_decompiled` - apktool output (resources, smali)
- `/captures` - Network traffic captures (HAR, mitmproxy flows)
- `/ha_integration` - Home Assistant custom component (WIP)
- `/docs` - API documentation and findings

## Key Findings
- **Tuya-based: NO** - Uses iRobotics/3irobotix custom cloud (same company as iLife)
- **Cloud API:** WebSocket to `eu.fas.3irobotics.net` (Europe) with JSON messages
- **Auth mechanism:** Username/password login via WebSocket, token-based session
- **Local protocol:** Aliyun CoAP for provisioning only; no direct local device control discovered
- **Device provisioning:** Aliyun IoT SDK (app key: `28416395`, product key: `a1wzCC1Mr2b`)
- **Map data:** Binary WebSocket frames with custom fragmented packet protocol
- **Full protocol documented in:** `docs/api_spec.md`

## Conventions
- Document all discovered endpoints in docs/api_spec.md
- Python code follows Home Assistant coding standards
- Test API calls with standalone scripts in /scripts before integrating
