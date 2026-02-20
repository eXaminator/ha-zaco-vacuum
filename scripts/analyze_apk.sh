#!/usr/bin/env bash
#
# analyze_apk.sh - Decompile a ZACO APK and analyze for Tuya SDK indicators
#
# Usage: ./scripts/analyze_apk.sh <path_to_apk_or_xapk>
#
# This script:
#   1. Extracts XAPK if needed (XAPK is just a zip with split APKs)
#   2. Decompiles with jadx (Java source) and apktool (resources/smali)
#   3. Searches for Tuya SDK indicators
#   4. Extracts embedded API keys and endpoints
#   5. Outputs a summary report
#
# Prerequisites: brew install jadx apktool

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
JADX_OUTPUT="$PROJECT_DIR/apk_jadx_output"
APKTOOL_OUTPUT="$PROJECT_DIR/apk_decompiled"
REPORT_FILE="$PROJECT_DIR/docs/apk_analysis_report.md"

# Colors for terminal output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log_info()  { echo -e "${BLUE}[INFO]${NC} $*"; }
log_ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
log_error() { echo -e "${RED}[ERROR]${NC} $*"; }
log_section() { echo -e "\n${BLUE}========================================${NC}"; echo -e "${BLUE} $*${NC}"; echo -e "${BLUE}========================================${NC}"; }

if [ $# -lt 1 ]; then
    echo "Usage: $0 <path_to_apk_or_xapk>"
    echo ""
    echo "Examples:"
    echo "  $0 apk_raw/ZACOHome.xapk"
    echo "  $0 apk_raw/ZACOHome.apk"
    exit 1
fi

INPUT_FILE="$1"

if [ ! -f "$INPUT_FILE" ]; then
    log_error "File not found: $INPUT_FILE"
    exit 1
fi

# Check prerequisites
for cmd in jadx apktool; do
    if ! command -v "$cmd" &> /dev/null; then
        log_error "$cmd is not installed. Run: brew install $cmd"
        exit 1
    fi
done

# ============================================================
# Step 1: Extract APK from XAPK if needed
# ============================================================
log_section "Step 1: Preparing APK"

APK_FILE=""
EXTENSION="${INPUT_FILE##*.}"

if [ "$EXTENSION" = "xapk" ] || [ "$EXTENSION" = "apks" ]; then
    log_info "Detected XAPK/APKS format, extracting..."
    EXTRACT_DIR="$PROJECT_DIR/apk_raw/extracted"
    rm -rf "$EXTRACT_DIR"
    mkdir -p "$EXTRACT_DIR"
    unzip -q "$INPUT_FILE" -d "$EXTRACT_DIR"

    # Find the base APK (the one containing DEX files with actual code)
    if [ -f "$EXTRACT_DIR/base.apk" ]; then
        APK_FILE="$EXTRACT_DIR/base.apk"
    else
        # Find the APK that contains classes.dex (the main code APK)
        # Config split APKs (config.*.apk) have hasCode="false" and no DEX
        APK_FILE=""
        for candidate in "$EXTRACT_DIR"/*.apk; do
            if unzip -l "$candidate" 2>/dev/null | grep -q "classes\.dex"; then
                APK_FILE="$candidate"
                break
            fi
        done
        # Fallback: look for package-named APK
        if [ -z "$APK_FILE" ]; then
            APK_FILE=$(find "$EXTRACT_DIR" -name "*.apk" -not -name "config.*" -type f | head -1)
        fi
    fi

    if [ -z "$APK_FILE" ] || [ ! -f "$APK_FILE" ]; then
        log_error "Could not find base APK inside XAPK. Contents:"
        ls -la "$EXTRACT_DIR"
        exit 1
    fi

    log_ok "Found base APK: $APK_FILE"

    # List all APKs in the XAPK (for reference)
    log_info "All APKs in XAPK:"
    find "$EXTRACT_DIR" -name "*.apk" -type f | while read -r f; do
        echo "  - $(basename "$f") ($(du -h "$f" | cut -f1))"
    done
else
    APK_FILE="$INPUT_FILE"
    log_ok "Using APK directly: $APK_FILE"
fi

# ============================================================
# Step 2: Decompile with jadx
# ============================================================
log_section "Step 2: Decompiling with jadx (Java source)"

if [ -d "$JADX_OUTPUT" ] && [ "$(ls -A "$JADX_OUTPUT" 2>/dev/null)" ]; then
    log_warn "jadx output directory already has content. Skipping decompilation."
    log_warn "Delete $JADX_OUTPUT to re-decompile."
else
    rm -rf "$JADX_OUTPUT"
    log_info "Running jadx (this may take a few minutes)..."
    jadx --show-bad-code -d "$JADX_OUTPUT" "$APK_FILE" 2>&1 | tail -5
    log_ok "jadx decompilation complete: $JADX_OUTPUT"
fi

# ============================================================
# Step 3: Decompile with apktool
# ============================================================
log_section "Step 3: Decompiling with apktool (resources + smali)"

if [ -d "$APKTOOL_OUTPUT" ] && [ "$(ls -A "$APKTOOL_OUTPUT" 2>/dev/null)" ]; then
    log_warn "apktool output directory already has content. Skipping decompilation."
    log_warn "Delete $APKTOOL_OUTPUT to re-decompile."
else
    rm -rf "$APKTOOL_OUTPUT"
    log_info "Running apktool..."
    apktool d "$APK_FILE" -o "$APKTOOL_OUTPUT" 2>&1 | tail -5
    log_ok "apktool decompilation complete: $APKTOOL_OUTPUT"
fi

# ============================================================
# Step 4: Tuya SDK Detection
# ============================================================
log_section "Step 4: Analyzing for Tuya SDK indicators"

TUYA_SCORE=0
TUYA_EVIDENCE=""

add_evidence() {
    TUYA_SCORE=$((TUYA_SCORE + 1))
    TUYA_EVIDENCE="${TUYA_EVIDENCE}\n- $1"
    log_ok "FOUND: $1"
}

# 4a. Check for com.tuya / com.thingclever packages
log_info "Checking for Tuya/Thing package references..."
TUYA_PACKAGES=$(grep -rl "com\.tuya\|com\.thingclever\|com\.thing\.smart" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | wc -l | tr -d ' ')
if [ "$TUYA_PACKAGES" -gt 0 ]; then
    add_evidence "Found com.tuya/com.thingclever references in $TUYA_PACKAGES Java files"
fi

# 4b. Check for Tuya native libraries
log_info "Checking for Tuya native libraries..."
if [ -d "$APKTOOL_OUTPUT/lib" ]; then
    TUYA_LIBS=$(find "$APKTOOL_OUTPUT/lib" -name "libtuya*" -o -name "libthing*" -o -name "libsmartlife*" 2>/dev/null | head -20)
    if [ -n "$TUYA_LIBS" ]; then
        add_evidence "Found Tuya native libraries: $(echo "$TUYA_LIBS" | xargs -I{} basename {} | tr '\n' ', ')"
    fi
fi

# 4c. Check AndroidManifest for Tuya components
log_info "Checking AndroidManifest.xml for Tuya components..."
if [ -f "$APKTOOL_OUTPUT/AndroidManifest.xml" ]; then
    MANIFEST_TUYA=$(grep -ci "tuya\|thingclever\|thing\.smart\|smartlife" "$APKTOOL_OUTPUT/AndroidManifest.xml" 2>/dev/null || echo "0")
    if [ "$MANIFEST_TUYA" -gt 0 ]; then
        add_evidence "Found $MANIFEST_TUYA Tuya-related references in AndroidManifest.xml"
    fi
fi

# 4d. Check for Tuya MQTT endpoints
log_info "Checking for Tuya MQTT endpoints..."
MQTT_HITS=$(grep -rl "m[12]\.tuya\|mq\.tuya\|mqtt.*tuya\|tuya.*mqtt" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | wc -l | tr -d ' ')
if [ "$MQTT_HITS" -gt 0 ]; then
    add_evidence "Found Tuya MQTT endpoint references in $MQTT_HITS files"
fi

# 4e. Check for TuyaSmart SDK initialization
log_info "Checking for TuyaSmart SDK init patterns..."
SDK_INIT=$(grep -rl "TuyaSmart\|ThingSmartSdk\|TuyaHomeSdk\|ThingHomeSdk" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | wc -l | tr -d ' ')
if [ "$SDK_INIT" -gt 0 ]; then
    add_evidence "Found TuyaSmart/ThingSmart SDK references in $SDK_INIT files"
fi

# 4f. Check for Tuya BizBundle
log_info "Checking for Tuya BizBundle..."
BIZBUNDLE=$(grep -rl "BizBundle\|bizbundle" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | wc -l | tr -d ' ')
if [ "$BIZBUNDLE" -gt 0 ]; then
    add_evidence "Found Tuya BizBundle references in $BIZBUNDLE files"
fi

# ============================================================
# Step 5: Extract useful information
# ============================================================
log_section "Step 5: Extracting keys, endpoints, and configuration"

# 5a. App keys
log_info "Searching for embedded app keys..."
APP_KEYS=$(grep -rn "appKey\|app_key\|clientId\|AppKey\|APP_KEY" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | grep -v "import\|//\|/\*" | head -20)

# 5b. API base URLs
log_info "Searching for API base URLs..."
API_URLS=$(grep -rn "https\?://[a-zA-Z0-9._-]*\.\(tuya\|tuyaeu\|tuyaus\|tuyacn\|smartlife\|zaco\)" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | head -20)

# 5c. All HTTP(S) URLs
log_info "Collecting all embedded URLs..."
ALL_URLS=$(grep -roh "https\?://[a-zA-Z0-9./_-]*" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | sort -u | head -50)

# 5d. Package name and version from manifest
PACKAGE_NAME=""
VERSION_NAME=""
if [ -f "$APKTOOL_OUTPUT/apktool.yml" ]; then
    VERSION_NAME=$(grep "versionName" "$APKTOOL_OUTPUT/apktool.yml" 2>/dev/null | head -1 | sed "s/.*versionName: *'\{0,1\}\([^']*\)'\{0,1\}/\1/" || echo "unknown")
fi
if [ -f "$APKTOOL_OUTPUT/AndroidManifest.xml" ]; then
    PACKAGE_NAME=$(grep "package=" "$APKTOOL_OUTPUT/AndroidManifest.xml" 2>/dev/null | head -1 | sed 's/.*package="\([^"]*\)".*/\1/' || echo "unknown")
fi

# 5e. Retrofit API interfaces (if non-Tuya or hybrid)
log_info "Searching for Retrofit API definitions..."
RETROFIT_APIS=$(grep -rn "@GET\|@POST\|@PUT\|@DELETE\|@PATCH" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | head -30)

# 5f. MQTT topics
log_info "Searching for MQTT topics..."
MQTT_TOPICS=$(grep -rn "subscribe\|topic\|mqtt" "$JADX_OUTPUT" --include="*.java" 2>/dev/null | grep -i "topic\|subscribe" | grep -v "import\|//\|/\*" | head -20)

# ============================================================
# Step 6: Generate Report
# ============================================================
log_section "Step 6: Generating analysis report"

cat > "$REPORT_FILE" << REPORT_EOF
# APK Analysis Report

Generated: $(date '+%Y-%m-%d %H:%M:%S')
Input file: $INPUT_FILE
Package: $PACKAGE_NAME
Version: $VERSION_NAME

## Tuya SDK Detection

**Score: $TUYA_SCORE / 6** (indicators found)

$(if [ "$TUYA_SCORE" -ge 3 ]; then
    echo "**VERDICT: HIGHLY LIKELY TUYA-BASED**"
elif [ "$TUYA_SCORE" -ge 1 ]; then
    echo "**VERDICT: POSSIBLY TUYA-BASED** (needs further investigation)"
else
    echo "**VERDICT: LIKELY NOT TUYA-BASED** (no Tuya indicators found)"
fi)

### Evidence
$(echo -e "$TUYA_EVIDENCE")

## Embedded App Keys

\`\`\`
$APP_KEYS
\`\`\`

## API URLs Found

### Tuya/ZACO-specific URLs
\`\`\`
$API_URLS
\`\`\`

### All Embedded URLs
\`\`\`
$ALL_URLS
\`\`\`

## Retrofit API Endpoints
\`\`\`
$RETROFIT_APIS
\`\`\`

## MQTT Topics
\`\`\`
$MQTT_TOPICS
\`\`\`

## File Statistics

- jadx Java files: $(find "$JADX_OUTPUT" -name "*.java" 2>/dev/null | wc -l | tr -d ' ')
- apktool smali files: $(find "$APKTOOL_OUTPUT" -name "*.smali" 2>/dev/null | wc -l | tr -d ' ')
- Native libraries: $(find "$APKTOOL_OUTPUT/lib" -name "*.so" 2>/dev/null | wc -l | tr -d ' ')
- Resources: $(find "$APKTOOL_OUTPUT/res" -type f 2>/dev/null | wc -l | tr -d ' ')

## Next Steps

$(if [ "$TUYA_SCORE" -ge 3 ]; then
    echo "1. Set up Tuya IoT Platform account at https://iot.tuya.com/"
    echo "2. Create Cloud Project (Central Europe data center)"
    echo "3. Link ZACOHome app account to get device visibility"
    echo "4. Run \`python -m tinytuya wizard\` to get device IDs and local keys"
    echo "5. Run \`python -m tinytuya scan\` to discover devices on local network"
    echo "6. Run \`python scripts/monitor_dps.py\` to passively monitor DPS changes"
elif [ "$TUYA_SCORE" -ge 1 ]; then
    echo "1. Investigate further - check native libraries and smali code"
    echo "2. Consider traffic capture with mitmproxy for additional evidence"
    echo "3. Look for hybrid architecture (Tuya SDK + custom cloud)"
else
    echo "1. Deep-dive into Retrofit API interfaces"
    echo "2. Set up mitmproxy traffic capture"
    echo "3. Analyze authentication flow in decompiled source"
    echo "4. Map all API endpoints manually"
fi)
REPORT_EOF

log_ok "Report saved to: $REPORT_FILE"

# ============================================================
# Summary
# ============================================================
log_section "SUMMARY"

echo -e "Package: ${GREEN}$PACKAGE_NAME${NC}"
echo -e "Version: ${GREEN}$VERSION_NAME${NC}"
echo ""

if [ "$TUYA_SCORE" -ge 3 ]; then
    echo -e "Tuya Detection: ${GREEN}HIGHLY LIKELY ($TUYA_SCORE/6 indicators)${NC}"
    echo -e "Evidence:${TUYA_EVIDENCE}"
    echo ""
    echo -e "${GREEN}Good news! This appears to be Tuya-based.${NC}"
    echo "This means we can use tinytuya for local control and"
    echo "potentially use the tuya-local HA integration."
elif [ "$TUYA_SCORE" -ge 1 ]; then
    echo -e "Tuya Detection: ${YELLOW}POSSIBLY TUYA ($TUYA_SCORE/6 indicators)${NC}"
    echo -e "Evidence:${TUYA_EVIDENCE}"
    echo ""
    echo "More investigation needed. Check the full report."
else
    echo -e "Tuya Detection: ${RED}NOT DETECTED (0/6 indicators)${NC}"
    echo ""
    echo "This does not appear to be Tuya-based."
    echo "Full API reverse engineering will be needed."
fi

echo ""
echo "Full report: $REPORT_FILE"
echo "Decompiled Java source: $JADX_OUTPUT"
echo "Decompiled resources: $APKTOOL_OUTPUT"
