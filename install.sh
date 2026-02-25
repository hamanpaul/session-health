#!/usr/bin/env bash
# session-health installer
# 自動偵測 ~/.paul_tools 存在與否，建立 symlink 或提示使用方式。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRY="${SCRIPT_DIR}/eval_session.py"
CMD_NAME="session-health"
PAUL_TOOLS="${HOME}/.paul_tools"

echo "╔════════════════════════════════════════╗"
echo "║  session-health installer              ║"
echo "╚════════════════════════════════════════╝"
echo ""

# Ensure entry point is executable
chmod +x "${ENTRY}"

# Check Python version
if ! command -v python3 &>/dev/null; then
    echo "❌ python3 not found. Please install Python 3.8+."
    exit 1
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 8 ]; }; then
    echo "❌ Python 3.8+ required (found ${PY_VER})."
    exit 1
fi
echo "✓ Python ${PY_VER} detected"

# Deploy
if [ -d "${PAUL_TOOLS}" ]; then
    # ~/.paul_tools exists — create symlink there
    LINK="${PAUL_TOOLS}/${CMD_NAME}"
    if [ -L "${LINK}" ] || [ -e "${LINK}" ]; then
        rm -f "${LINK}"
        echo "  (removed existing ${LINK})"
    fi
    ln -s "${ENTRY}" "${LINK}"
    echo "✓ Symlink created: ${LINK} → ${ENTRY}"
    echo ""

    # Check if PAUL_TOOLS is in PATH
    if echo "$PATH" | tr ':' '\n' | grep -qx "${PAUL_TOOLS}"; then
        echo "✓ ${PAUL_TOOLS} is in PATH"
        echo "  Run:  ${CMD_NAME} <session-id>"
    else
        echo "⚠ ${PAUL_TOOLS} is NOT in your PATH."
        echo "  Add to ~/.bashrc:"
        echo "    export PATH=\"${PAUL_TOOLS}:\$PATH\""
        echo ""
        echo "  Or run directly:"
        echo "    ${LINK} <session-id>"
    fi
else
    # No ~/.paul_tools — use project directory
    echo "ℹ ${PAUL_TOOLS} not found."
    echo "  Using project directory as execution path."
    echo ""
    echo "  Run with:"
    echo "    ${ENTRY} <session-id>"
    echo ""
    echo "  Or create an alias in ~/.bashrc:"
    echo "    alias ${CMD_NAME}='${ENTRY}'"
fi

echo ""
echo "Done. Run '${CMD_NAME} --help' for usage."
