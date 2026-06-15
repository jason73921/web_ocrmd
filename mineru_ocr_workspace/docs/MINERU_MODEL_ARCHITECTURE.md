# MinerU Model Architecture Used in the Platform Run

This document records the MinerU model/pipeline structure observed on the Linux H200 platform for the completed OCR run.

## Runtime

| Item | Value |
|---|---|
| Python | 3.12.3 |
| MinerU API | 3.1.15 |
| PyTorch | 2.9.0+cu128 |
| CUDA | 12.8 |
| GPU | NVIDIA H200 NVL |
| MinerU device | `cuda` |
| ONNX Runtime providers | `AzureExecutionProvider`, `CPUExecutionProvider` |

Important distinction:

- MinerU's main document analysis pipeline ran on CUDA. Logs showed `GPU Memory: 137 GB, Batch Ratio: 16`, `Layout Predict`, `OCR-det chinese_cht`, and `OCR-rec Predict` during processing.
- ONNX Runtime was CPU-only in this environment. In this run, the main performance bottleneck was mixed CPU/GPU pipeline work, not CUDA being unavailable.

## Model Package

The platform had the MinerU model package available at:

```text
/mnt/project/models/PDF-Extract-Kit-1.0
/root/.cache/huggingface/hub/models--opendatalab--PDF-Extract-Kit-1.0
```

Do not commit this model package to git. It contains large model weights (`.safetensors`, `.pth`, `.onnx`).

## Pipeline Components

Observed model files under `/mnt/project/models/PDF-Extract-Kit-1.0/models`:

| Component | Model files / directory | Role |
|---|---|---|
| Layout detection | `Layout/PP-DocLayoutV2/model.safetensors` | Detect document layout regions such as text, headings, images, tables, formulas |
| OCR detection | `OCR/paddleocr_torch/ch_PP-OCRv5_det_infer.pth` | Detect Chinese text boxes/regions |
| OCR recognition | `OCR/paddleocr_torch/ch_PP-OCRv5_rec_server_infer.pth`, `ch_PP-OCRv5_rec_infer.pth`, PP-OCRv4 variants | Recognize text from detected boxes |
| Seal detection/recognition support | `OCR/paddleocr_torch/seal_PP-OCRv4_det_infer.pth`, `seal_PP-OCRv4_det_server_infer.pth` | Detect seal/stamp-like regions when present |
| Formula recognition | `MFR/unimernet_hf_small_2503/model.safetensors` plus tokenizer/config files | Convert mathematical formula regions into text/LaTeX-like output |
| Table classification | `TabCls/paddle_table_cls/PP-LCNet_x1_0_table_cls.onnx` | Classify table-related regions |
| Table structure recognition | `TabRec/SlanetPlus/slanet-plus.onnx`, `TabRec/UnetStructure/unet.onnx` | Recover structured table layout |
| File type detection | `magika` package model cache | Preflight / file type identification before OCR submission |

## Processing Flow

The FastAPI pipeline processes each submitted batch roughly as:

1. Load and preflight the input file.
2. Convert PDF/document pages or images into page images.
3. Run layout analysis.
4. Run OCR detection and recognition for selected regions.
5. Run MFR for formula regions when detected.
6. Optionally run table classification/structure recognition.
7. Build MinerU raw output and Markdown.
8. Copy the selected Markdown output into `ocr_md_assets`.

## Why GPU Utilization Is Bursty

The pipeline is not a single dense GPU kernel. Several stages are CPU/IO-heavy:

- PDF rasterization and document loading
- ZIP/result extraction and file copying
- Markdown generation/post-processing
- Some ONNX/table/file-type operations
- Per-page orchestration and multiprocessing overhead

On the platform, `cpu.max` showed roughly 11 CPU cores. With 4 parallel API workers, CPU usage was often saturated while GPU usage appeared in bursts. That is expected for this mixed pipeline.

## Table Extraction Decision

The production run used `--disable-table`.

Reason:

- Full table structure extraction was the main slow/crash path in this dataset.
- Earlier tests showed native crashes such as `std::bad_alloc` / allocator corruption during full table-heavy workloads.
- Disabling table structure extraction preserved OCR text extraction while making the run stable enough to finish all pending assets.

Impact:

- Text OCR content is available.
- Fine-grained structured table reconstruction is not guaranteed in the final Markdown.

## Final OCR Execution Parameters

The stable production run used:

```text
workers = 4
rows-per-worker = 100
batch-size = 16
processing-window-size = 16
timeout-sec = 1800
chunk-timeout-sec = 7200
disable-table = true
language = chinese_cht
backend = pipeline
```

Final OCR status:

```text
completed_cached: 24,261
completed: 3,147
error: 70
pending: 0
running: 0
```
