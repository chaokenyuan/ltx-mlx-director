#!/usr/bin/env bash
# LTX-2.3 導演級單鏡 / 分鏡生成腳本
set -euo pipefail

# ---------- 預設值 ----------
DURATION=4
FPS=24
ASPECT="16:9"
SEED=-1
MODE="fast"        # fast | hq
IMAGE=""
ENHANCE=0
STORYBOARD=""
MODEL="${MODEL:-dgrauet/ltx-2.3-mlx-q4}"
EXTRA_ARGS="${EXTRA_ARGS:-}"
REPO_DIR="$HOME/ltx-2-mlx"
OUT_DIR="$REPO_DIR/output"

usage() {
  cat <<EOF
用法:
  $(basename "$0") [選項] "<prompt>" [輸出名]
  $(basename "$0") [選項] --storyboard <檔> [輸出名]

時長與節奏:
  -d, --duration SEC    片長秒數（預設 4，會對齊到 1+8N 幀）
  -f, --fps RATE        幀率（預設 24；24 電影 / 30 標準 / 60 流暢）

構圖:
  -a, --aspect AR       16:9 (預設) | 9:16 | 1:1 | 21:9 | 4:5

控制:
  -s, --seed N          隨機種子（預設 -1 即隨機）
  -m, --mode MODE       fast = distilled (約 1 分鐘)
                        hq   = two-stages-hq (約 5+ 分鐘，更精緻)
  -i, --image PATH      i2v 起手圖
      --enhance         用 Gemma 改寫 prompt

分鏡:
      --storyboard FILE 多鏡頭檔；每行一鏡，行首 [N] 指定該鏡秒數
                        範例：
                          [3] 廣角航拍：海邊夕陽
                          [5] 推近：礁石上的海鳥起飛
                          [2] 特寫：浪花拍岸

環境變數覆寫:
  MODEL=...             預設 dgrauet/ltx-2.3-mlx-q4
  EXTRA_ARGS="..."      附加 ltx-2-mlx 旗標（如 --cfg-scale 4.0）

輸出:
  單鏡: ~/ltx-2-mlx/output/<name>.mp4
  分鏡: ~/ltx-2-mlx/output/<name>_NN.mp4+ <name>.mp4（串接）
EOF
}

# ---------- 參數解析 ----------
POSITIONAL=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    -d|--duration) DURATION="$2"; shift 2 ;;
    -f|--fps)      FPS="$2"; shift 2 ;;
    -a|--aspect)   ASPECT="$2"; shift 2 ;;
    -s|--seed)     SEED="$2"; shift 2 ;;
    -m|--mode)     MODE="$2"; shift 2 ;;
    -i|--image)    IMAGE="$2"; shift 2 ;;
    --enhance)     ENHANCE=1; shift ;;
    --storyboard)  STORYBOARD="$2"; shift 2 ;;
    --) shift; while [ $# -gt 0 ]; do POSITIONAL+=("$1"); shift; done ;;
    -*) echo "未知選項: $1" >&2; usage; exit 1 ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done

# ---------- 構圖 → 解析度（皆為 32 倍數，貼近 LTX 訓練解析度）----------
aspect_to_wh() {
  case "$1" in
    16:9) echo "704 384" ;;
    9:16) echo "384 704" ;;
    1:1)  echo "512 512" ;;
    21:9) echo "704 288" ;;
    4:5)  echo "480 608" ;;
    *) echo "未支援的 aspect: $1（請用 16:9 / 9:16 / 1:1 / 21:9 / 4:5）" >&2; exit 1 ;;
  esac
}

# ---------- 時長 → 對齊幀數（LTX 要 1 + 8N）----------
duration_to_frames() {
  local sec="$1" fps="$2"
  local target n frames
  target=$(awk "BEGIN{printf \"%d\", $sec * $fps}")
  n=$(awk "BEGIN{printf \"%d\", ($target - 1 + 4) / 8}")
  [ "$n" -lt 1 ] && n=1
  frames=$((1 + n * 8))
  echo "$frames"
}

# ---------- 模式 → pipeline flag ----------
mode_flag() {
  case "$1" in
    fast) echo "--distilled" ;;
    hq)   echo "--two-stages-hq" ;;
    *) echo "未知 mode: $1（fast | hq）" >&2; exit 1 ;;
  esac
}

# ---------- 單鏡執行 ----------
run_shot() {
  local prompt="$1" out_path="$2" sec="$3"
  local frames width height pipe
  read -r width height < <(aspect_to_wh "$ASPECT")
  frames=$(duration_to_frames "$sec" "$FPS")
  pipe=$(mode_flag "$MODE")

  local image_args=()
  [ -n "$IMAGE" ] && image_args=(--image "$IMAGE")
  local enhance_args=()
  [ "$ENHANCE" -eq 1 ] && enhance_args=(--enhance-prompt)

  echo "  ── shot: \"$prompt\""
  echo "     size=${width}x${height}  frames=${frames}  fps=${FPS}  seed=${SEED}  mode=${MODE}"

  cd "$REPO_DIR"
  # shellcheck disable=SC2086
  uv run ltx-2-mlx generate \
    -p "$prompt" \
    $pipe --low-ram \
    --width "$width" --height "$height" \
    --frames "$frames" \
    --frame-rate "$FPS" \
    --seed "$SEED" \
    --model "$MODEL" \
    "${image_args[@]}" "${enhance_args[@]}" \
    -o "$out_path" \
    $EXTRA_ARGS
}

mkdir -p "$OUT_DIR"

# ---------- 分支：分鏡 or 單鏡 ----------
if [ -n "$STORYBOARD" ]; then
  [ ! -f "$STORYBOARD" ] && { echo "分鏡檔不存在: $STORYBOARD" >&2; exit 1; }
  BASE="${POSITIONAL[0]:-$(date +%Y%m%d-%H%M%S)}"
  BASE="${BASE%.mp4}"

  echo "分鏡模式 - 來源: $STORYBOARD"
  echo "輸出基底: $OUT_DIR/${BASE}_NN.mp4"
  echo "============================================"

  CONCAT_LIST="$OUT_DIR/${BASE}.concat.txt"
  : > "$CONCAT_LIST"

  shot_idx=0
  total_start=$(date +%s)
  while IFS= read -r raw || [ -n "$raw" ]; do
    line="$(echo "$raw" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "$line" ] && continue
    case "$line" in '#'*) continue ;; esac

    sec="$DURATION"
    if [[ "$line" =~ ^\[([0-9]+(\.[0-9]+)?)\][[:space:]]*(.*)$ ]]; then
      sec="${BASH_REMATCH[1]}"
      prompt="${BASH_REMATCH[3]}"
    else
      prompt="$line"
    fi
    [ -z "$prompt" ] && continue

    shot_idx=$((shot_idx + 1))
    nn=$(printf "%02d" "$shot_idx")
    shot_path="$OUT_DIR/${BASE}_${nn}.mp4"

    echo
    echo "[Shot $nn / ${sec}s]"
    shot_start=$(date +%s)
    run_shot "$prompt" "$shot_path" "$sec"
    shot_end=$(date +%s)
    echo "     -> $shot_path  ($((shot_end - shot_start))s)"
    echo "file '$shot_path'" >> "$CONCAT_LIST"
  done < "$STORYBOARD"

  [ "$shot_idx" -eq 0 ] && { echo "分鏡檔無有效行" >&2; exit 1; }

  FINAL="$OUT_DIR/${BASE}.mp4"
  echo
  echo "============================================"
  echo "串接 $shot_idx 鏡 -> $FINAL"
  ffmpeg -y -hide_banner -loglevel warning \
    -f concat -safe 0 -i "$CONCAT_LIST" \
    -c copy "$FINAL"

  total_end=$(date +%s)
  echo "完成於 $((total_end - total_start)) 秒"
  ls -lh "$FINAL"
else
  # 單鏡
  [ ${#POSITIONAL[@]} -lt 1 ] && { usage; exit 1; }
  PROMPT="${POSITIONAL[0]}"
  OUT_NAME="${POSITIONAL[1]:-$(date +%Y%m%d-%H%M%S)}"
  OUT_NAME="${OUT_NAME%.mp4}.mp4"
  OUT_PATH="$OUT_DIR/$OUT_NAME"

  start=$(date +%s)
  run_shot "$PROMPT" "$OUT_PATH" "$DURATION"
  end=$(date +%s)
  echo "============================================"
  echo "完成於 $((end - start)) 秒  ->  $OUT_PATH"
  ls -lh "$OUT_PATH"
fi
