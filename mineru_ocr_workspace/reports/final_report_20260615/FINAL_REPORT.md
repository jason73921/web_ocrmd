# Web Markdown OCR 最終報告

產生時間：2026-06-15T04:16:26

## 一、資料位置

| 類型 | 路徑 |
|---|---|
| 原始 Web Markdown | `/mnt/project/input/md/網站文章MD_更新/網站文章MD` |
| 關聯檔案掃描 manifest | `/mnt/project/cache/manifest_package/related_assets_manifest.csv` |
| OCR 主狀態 CSV | `/mnt/project/output/ocr_run/ocr_asset_status.csv` |
| 本次平台新增 OCR Markdown | `/mnt/project/output/ocr_run/ocr_md_assets` |
| 既有 OCR 快取 Markdown | `/mnt/project/cache/ocr_completed_cache/ocr_md_assets` |
| MinerU raw output | `/mnt/project/output/ocr_run/mineru_raw` |
| 最終 inline 合併 Markdown | `/mnt/project/output/web_md_with_ocr_inline_20260615` |
| 文末集中版合併 Markdown（保留不用） | `/mnt/project/output/web_md_with_ocr_20260615` |
| 本報告資料夾 | `/mnt/project/output/final_report_20260615` |

## 二、Web Markdown 掃描總結

| 指標 | 數量 |
|---|---:|
| 原始 Web Markdown 檔案 | 34,144 |
| 有至少一筆關聯連結的 Markdown | 21,250 |
| 沒有掃到關聯連結的 Markdown | 12,894 |
| 關聯連結總筆數（manifest rows） | 50,610 |
| 可解析到本機檔案/資產的連結 | 42,615 |
| 解析不到、格式不對、或非本機檔案的連結 | 7,995 |
| 可解析且副檔名可 OCR 的關聯記錄 | 38,227 |
| 可解析但副檔名/類型不進 OCR 的關聯記錄 | 4,388 |
| 去重後可 OCR 的唯一資產 | 27,478 |

## 三、連結解析狀態

| resolveStatus | 筆數 |
|---|---:|
| resolved | 42615 |
| confirmed_missing | 4333 |
| not_file_or_malformed | 3502 |
| ambiguous | 160 |

### 解析不到的連結

完整清單：`/mnt/project/output/final_report_20260615/unresolved_links.csv`

| 分類 | 筆數 |
|---|---:|
| 解析不到但副檔名原本可 OCR | 3,909 |
| 解析不到且本來就不是 OCR 支援類型 | 4,086 |

解析不到但原本可 OCR 的清單：`/mnt/project/output/final_report_20260615/unresolved_ocr_supported_links.csv`

解析不到且不可 OCR 的清單：`/mnt/project/output/final_report_20260615/unresolved_not_ocr_supported_links.csv`

### 解析不到樣本

| md | index | 類型 | ext | status | URL |
|---|---:|---|---|---|---|
| `人事室_content_ 【轉知】桃園市政府教育局函，教育部國民及學前教育署與國泰人_291.md` | 1 | 超連結（檔案） | `(none)` | not_file_or_malformed | ht%20tps:/youtu.be/izi_0Q5qrSg |
| `人事室_content_ 元智大學 電機工程學系（丙組）誠徵專任教師  (刊登日期：_574.md` | 5 | 超連結（檔案） | `(none)` | not_file_or_malformed | index.php/tw/2016-03-17-06-14-27 |
| `人事室_content_ 元智大學工業工程與管理學系 誠徵專任教師 (刊登日期：11_566.md` | 6 | 超連結（檔案） | `(none)` | not_file_or_malformed | index.php/tw/2016-03-17-06-14-27 |
| `人事室_content_ 元智大學工業工程與管理學系 誠徵專任教師 (刊登日期：11_566.md` | 7 | 超連結（檔案） | `(none)` | not_file_or_malformed | index.php/tw/2016-03-17-06-14-27 |
| `人事室_content_【2024遠東精神獎】甄選辦法，敬請全校教職員踴躍申請或推薦_770.md` | 5 | 圖片 | `(none)` | not_file_or_malformed | 遠東精神獎提報案件數說明 |
| `人事室_content_【性別平等教育相關法規宣導】校園性別事件相關之教育人員法令_521.md` | 1 | 超連結（檔案） | `(none)` | not_file_or_malformed | index.php/tw/2016-03-18-03-47-16/2018-10-19-01-58-16 |
| `人事室_content_【性別平等教育相關法規宣導】校園性別事件相關之教育人員法令_667.md` | 1 | 超連結（檔案） | `(none)` | not_file_or_malformed | index.php/tw/2016-03-18-03-47-16/2018-10-19-01-58-16 |
| `人事室_content_【性別平等教育相關法規宣導】校園性別事件相關之教育人員法令_667.md` | 2 | 超連結（檔案） | `(none)` | not_file_or_malformed | index.php/tw/2016-03-18-03-47-16/2016-03-18-09-09-19 |
| `人事室_content_【轉知】為配合自然人憑證系統虛擬化上線作業，內政部自然人憑證_439.md` | 1 | 超連結（檔案） | `.tw` | not_file_or_malformed | icsmoica.moi.gov.tw |
| `人事室_content_110學年度第2學期全校各委員會委員名單_491.md` | 2 | 超連結（檔案） | `(none)` | not_file_or_malformed | index.php/tw/2016-03-17-06-57-02 |
| `人事室_content_112學年度全校各委員會委員名單_651.md` | 28 | 超連結（檔案） | `.pdf` | confirmed_missing | files/112學年度全校各委員會公告/112-113學年度計畫暨預算審查委員會委員名單.pdf |
| `人事室_content_中國語文學系_所  115-1學期誠徵專案教師(一年期)1名_1024.md` | 1 | 超連結（檔案） | `.pdf` | confirmed_missing | files/工作刊登/中語系徵聘教師基本資料表.pdf |

## 四、沒有關聯連結的 Markdown

沒有掃到任何關聯連結的 Markdown：12,894 個。

完整清單：`/mnt/project/output/final_report_20260615/md_without_related_links.csv`

這裡的「沒有關聯連結」是指掃描 manifest 沒有該 md 的關聯項目；不是指文章正文沒有任何網址文字。

## 五、不可 OCR 的連結/資產

| 類型 | 筆數 |
|---|---:|
| 所有不屬於 OCR 支援副檔名/類型的關聯記錄 | 8,474 |
| 其中已解析到本機但不進 OCR | 4,388 |
| 其中解析不到且也不進 OCR | 4,086 |

完整不可 OCR 清單：`/mnt/project/output/final_report_20260615/not_ocr_supported_links.csv`

已解析但不 OCR 清單：`/mnt/project/output/final_report_20260615/resolved_not_ocr_supported_links.csv`

## 六、OCR 執行結果

| OCR 狀態 | 唯一資產數 |
|---|---:|
| completed_cached | 24261 |
| completed | 3147 |
| error | 70 |

解讀：

- `completed_cached`：之前已存在 OCR 快取，本次不用重跑，共 24,261 個。
- `completed`：這次在平台上新增 OCR 成功，共 3,147 個。
- `error`：本次或狀態表中確認無法成功 OCR 的資產，共 70 個。
- 可用 OCR Markdown 總資產數：27,408 個。
- 完成狀態但 Markdown 檔不存在：0 個。

OCR 失敗完整清單：`/mnt/project/output/final_report_20260615/ocr_error_assets.csv`

完成但找不到 Markdown 的清單：`/mnt/project/output/final_report_20260615/completed_missing_markdown.csv`

### OCR 失敗原因分類

| errorCategory | 資產數 |
|---|---:|
| other | 40 |
| unsupported_file_type | 21 |
| extension_content_mismatch | 4 |
| password_or_encrypted_pdf | 2 |
| empty_file | 1 |
| corrupt_or_invalid_pdf | 1 |
| cannot_identify_image | 1 |

## 七、最終合併輸出

| 指標 | 數量 |
|---|---:|
| inline 合併輸出 md 檔案 | 34,144 |
| 有 inline OCR 區塊的 md | 18,120 |
| inline OCR 區塊數 | 38,144 |
| OCR 內容已內嵌 | 37,923 |
| 過大未內嵌、只保留 OCR Markdown 路徑 | 221 |
| 找不到對應 relation index | 0 |

最終應使用：`/mnt/project/output/web_md_with_ocr_inline_20260615`

這版 OCR 區塊放在每個 `## 二、關聯的檔案資訊` 裡對應連結的正下方，不是集中放在檔案文末。

## 八、附錄 CSV

| 檔案 | 內容 |
|---|---|
| `md_without_related_links.csv` | 沒有掃到關聯連結的 Markdown |
| `unresolved_links.csv` | 所有解析不到/非本機檔案/格式異常的關聯連結 |
| `unresolved_ocr_supported_links.csv` | 解析不到，但副檔名原本可 OCR 的關聯連結 |
| `unresolved_not_ocr_supported_links.csv` | 解析不到，且不是 OCR 支援類型的關聯連結 |
| `not_ocr_supported_links.csv` | 所有不進 OCR 的關聯連結 |
| `resolved_not_ocr_supported_links.csv` | 已解析到本機，但不進 OCR 的關聯連結 |
| `ocr_error_assets.csv` | OCR 失敗的唯一資產 |
| `completed_missing_markdown.csv` | 完成狀態但 Markdown 不存在的異常清單，應為 0 |
