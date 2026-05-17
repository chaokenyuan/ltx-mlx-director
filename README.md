# LTX-MLX Director

LTX-2.3 影片產生的「導演級」CLI 與 GUI，在 Apple Silicon Mac 本地以 MLX 執行。

包裝 [`dgrauet/ltx-2-mlx`](https://github.com/dgrauet/ltx-2-mlx)，提供：

- `gen.sh` — 友善的命令列，支援時長、構圖、模式、分鏡腳本檔
- `ui/app.py` — Gradio 面板，分鏡式編輯 + 進度追蹤 + 鏡頭串接

## 系統需求

| 項目 | 需求 |
|------|------|
| OS | macOS + Apple Silicon (M1 以上) |
| RAM | 16GB+（搭配 q4 + `--low-ram`），建議 32GB+ |
| 磁碟 | 12GB（fast 模式）/ 30GB+（hq + two-stage）/ 60GB+（全模式皆下載一次） |
| 工具 | `ffmpeg`、`uv`、Python 3.11+ |

## 安裝

```bash
# 1) 安裝底層 ltx-2-mlx 引擎（一次性）
git clone https://github.com/dgrauet/ltx-2-mlx.git ~/ltx-2-mlx
cd ~/ltx-2-mlx
uv sync --all-extras

# 2) clone 本 repo
git clone git@github.com:chaokenyuan/ltx-mlx-director.git
cd ltx-mlx-director
chmod +x gen.sh ui/run.sh
```

## CLI 使用

```bash
# 最短：一個 prompt
./gen.sh "一隻大白貓在草地上奔跑"

# 完整：6 秒、9:16 直拍、固定 seed、HQ 模式
./gen.sh -d 6 -a 9:16 -s 42 -m hq "高速公路夜景縮時，霓虹倒影" night

# 分鏡腳本（多鏡頭自動串接）
./gen.sh --storyboard examples/sunset.txt -a 21:9 sunset_final
```

分鏡檔格式（每行一鏡，`[N]` 為該鏡秒數）：

```text
[3] 廣角航拍：海邊夕陽，金色波光
[5] 推近：礁石上的海鳥拍翅起飛，慢動作
[2] 特寫：浪花拍上岩石碎成水霧
```

所有旗標：

```text
-d, --duration SEC    片長秒數（預設 4，會對齊到 1+8N 幀）
-f, --fps RATE        幀率（預設 24；24 電影 / 30 標準 / 60 流暢）
-a, --aspect AR       16:9 (預設) | 9:16 | 1:1 | 21:9 | 4:5
-s, --seed N          隨機種子（-1 隨機）
-m, --mode MODE       fast = distilled（約 1 分鐘）
                      hq   = two-stages-hq（5+ 分鐘，更精緻）
-i, --image PATH      i2v 起手圖
    --enhance         用 Gemma 改寫 prompt
    --storyboard FILE 多鏡頭檔；產出 NN.mp4 + 串接後最終片
```

環境變數：

```text
MODEL=...        預設 dgrauet/ltx-2.3-mlx-q4
EXTRA_ARGS="..." 附加 ltx-2-mlx 旗標（如 --cfg-scale 4.0）
```

## GUI 使用

```bash
./ui/run.sh
# 自動開啟瀏覽器到 http://127.0.0.1:7860
```

面板功能：

- 全域設定（aspect / fps / mode / seed / model / enhance）
- 編輯式分鏡表（新增 / 刪除 / 上下移動）
- 逐鏡進度條與 log 串流
- 完成鏡頭預覽（gallery）
- 一鍵 ffmpeg concat 出最終影片
- Storyboard JSON 匯出 / 匯入

## 解析度對照

每個 aspect 套用以下解析度（皆為 32 倍數，貼近 LTX 訓練尺寸）：

| aspect | width × height |
|--------|---------------|
| 16:9 | 704 × 384 |
| 9:16 | 384 × 704 |
| 1:1 | 512 × 512 |
| 21:9 | 704 × 288 |
| 4:5 | 480 × 608 |

## 限制與已知行為

- LTX-2.3 為單一鏡頭模型，**分鏡** 是逐鏡呼叫後 `ffmpeg concat` 串接的結果，並非單次多鏡頭生成。
- 串接要求所有鏡頭的解析度與 fps 一致；本工具已自動保證。
- 首次跑 `--distilled` 或 `--two-stages-hq` 會下載對應權重，分別約 12GB / 25GB+。
- 「秒數」會對齊到 `1 + 8N` 幀，實際長度可能略長於設定。

### 參考圖（i2v）保留策略

LTX-2.3 上游已知行為：所有 `generate` pipeline 中 `--image` 只作為「第 0 幀的初始化」，
之後 4 秒內影片會朝 prompt 描述的分佈飄走（即使設定 STG=1.0 也一樣）。

本工具的補救：當任一鏡頭含參考圖時，自動：

1. **覆寫 pipeline 為 `--two-stage`**（CFG + 半解析度雙階段 + distilled LoRA refine，q4 可用）— 因為 `--distilled` 無 CFG 圖像條件最弱，而 `--one-stage` 不支援多錨點 i2v。
2. **頭尾雙端錨點**：`--image PATH 0 1.0 --image PATH (frames-1) 1.0`，鎖住首尾兩幀為同一張圖。

代價：速度慢 2-3 倍（`--distilled` 約 1 分鐘 → `--two-stage` 約 2.5-5 分鐘）。
若需要極致的身份保留（畫面每一幀都被參考圖控制），需改用 `ic-lora` + canny edge —
此模式較重，尚未整合進 UI / CLI，計畫後續加入。

## 授權

MIT。底層引擎 `ltx-2-mlx` 與 LTX-2 模型權重各有授權，請依其條款使用。
