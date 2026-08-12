#!/bin/bash
# ILA skill 部署脚本 (Linux/macOS)
# 用法: bash scripts/deploy-skill.sh [平台...]
#   不传参数 = 部署到所有已安装平台; 可指定: hermes openclaw workbuddy opencode
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_SRC="$REPO_DIR/skills/ila/SKILL.md"

if [ ! -f "$SKILL_SRC" ]; then
    echo "❌ 未找到 $SKILL_SRC" >&2
    exit 1
fi

deploy() {
    local platform="$1" target_dir="$2"
    if [ -d "$target_dir" ] || [ "${FORCE:-0}" = "1" ]; then
        mkdir -p "$target_dir/ila"
        cp "$SKILL_SRC" "$target_dir/ila/SKILL.md"
        echo "✓ $platform: $target_dir/ila/SKILL.md"
    else
        echo "- $platform: 未安装，跳过"
    fi
}

PLATFORMS=("${@:-hermes openclaw workbuddy opencode}")

for p in $PLATFORMS; do
    case "$p" in
        hermes)   deploy hermes   "$HOME/.hermes/skills" ;;
        openclaw) deploy openclaw "$HOME/.openclaw/skills" ;;
        workbuddy) deploy workbuddy "$HOME/.workbuddy/skills" ;;
        opencode) deploy opencode "$HOME/.opencode/skills" ;;
        *) echo "❌ 未知平台: $p (可选: hermes openclaw workbuddy opencode)" >&2 ;;
    esac
done

echo ""
echo "✅ ILA skill 部署完成 (来源: $SKILL_SRC)"
