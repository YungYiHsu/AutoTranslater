# 電子書模式

網文與電子書是主 GUI 的兩個平行模式；OCR 是電子書取得原文的步驟，沒有獨立工具入口。

## 介面

- 作品設定：既有作品唯讀下拉選單、新增 BOOK☆WALKER 日本站作品、開啟作品資料夾。
- OCR：預覽、辨識與校訂、版面設定。載入圖片先定位，再由使用者開始辨識。
- 翻譯：檢視章節原文、貼入與儲存譯文。**LLM 尚未串接，不會自動發送原文。**

BOOK☆WALKER 僅讀取公開商品頁書目資料，不下載書籍正文。相同商品不重複建立或覆寫。
切換作品前檢查未儲存內容；辨識中不能切換作品。隱藏的 OCR 分頁延後至切入時刷新。

## 資料位置

`config.json` 的 `output_directory` 是兩種模式共用輸出根目錄，預設 `outputs`。

```text
outputs/
├─ webnovel/<網站>/<作品名稱>/
└─ ebook/bookwalker/<作品ID>/
   ├─ work.json
   ├─ chapters.json
   ├─ images/p010-p011.png
   ├─ ocr/<紀錄ID>/
   │  ├─ original.png
   │  ├─ manifest.json
   │  ├─ layout.json
   │  └─ crops/
   ├─ originals/001.txt
   └─ translations/001.txt

runtime/ocr/venv/             OCR Python 環境
runtime/ebook/                版面設定；未綁作品的內部測試紀錄
cache/ocr/                    Hugging Face / Torch 模型快取
checkpoints/webnovel/         網文續跑紀錄
private-tests/                私人標準答案與本機回歸測試產物
logs/ebook-worker.log         辨識背景程序日誌
```

頁碼至少三位，單頁 `p010.png`、雙頁 `p010-p011.png`，1000 以上不截斷。
匯出時填頭頁、尾頁（允許 0，尾頁不可小於頭頁），不提供單／雙頁選項。
頭尾相同使用單頁檔名，否則使用頭尾頁檔名。保存完整原圖，不縮放。
OCR 紀錄另保留 `original.png`，維持雜湊驗證與重開紀錄相容性。
截圖／載入時僅保存 OCR 備份，不建立正式圖片；匯出原文才寫入 `images/`。
預覽顯示「建議頁碼」：依正式圖片最大頁碼建議後續兩頁，版本檔不重複計算。
沒有圖片時顯示「沒有」，匯出預填 `001–002`；切換作品與成功匯出後更新。
頁碼重複可取消或另存 `_v02`、`_v03`，不覆寫。同份紀錄再匯出沿用已存圖片與頁碼，不重複存圖。

「匯出章節原文」依序填頭頁、尾頁、章節編號與起訖行號，不詢問章名。
章節編號允許 0，新檔名為 `000.txt`。校訂文字優先，否則使用原始 OCR。
所選範圍有未完成／失敗行時拒絕匯出。同章按頁碼與行序接合，重複匯出同份紀錄不重複追加。
原文改動後保留譯文並提示版本不同；按「儲存章節譯文」才寫入每章一個 TXT。

OCR 與校訂分開保存，每完成一行即更新 `manifest.json`；完成、取消或失敗時產生
`raw-columns.txt`、`raw-merged.txt` 與必要的 `corrected.txt`。逐行進度不依賴 TXT，
異常中斷後仍可從 manifest 續跑。取消辨識在目前行完成後生效。
框線使用原圖座標。Y 預設依 `38/1200`、`1168/1200` 高度比例取整；X 逐行定位，
不以固定 17／18 行為必要條件。插圖、短行與振假名仍須人工檢查。

正式 GUI 不提供「匯入舊紀錄」。預覽頁的「清空 OCR 紀錄」僅清除目前作品 `ocr/`，
不影響 `images/`、章節原文與譯文。清除前確認，辨識中禁用，清除後清空目前畫面。
舊有帶章名檔案仍可讀取；再次匯出該章時改用純編號檔名，複製既有譯文並保留舊檔。
若新檔名已有不相容內容則拒絕覆寫。
舊 `.local-ocr/venv` 與舊測試紀錄已移除；目前僅使用 `runtime/ocr/venv`。

## 背景工作與儲存

- 圖片讀取、備份、逐行偵測、裁切、OCR、正式匯出與清除紀錄在背景執行；
  Tk 元件與確認對話框只由主執行緒更新。處理中禁止切換作品及修改相關設定。
- 灰階、連通元件與插圖／標題分析共用同一次結果；逐行模式不再執行舊 17／18 行網格推算。
- OCR 子程序預設模型載入上限 300 秒、單次辨識上限 60 秒，可透過
  `ProcessRecognizer(load_timeout=..., recognition_timeout=...)` 調整。
  逾時終止本程式啟動的 OCR 子程序，保留完成結果及其餘待處理行，下次續跑重新啟動模型。
- 章節匯出分為準備與提交；確認對話框後沿用同一份合併結果，若期間章節索引變更則拒絕提交。
  `chapters.json` 格式維持相容，並未遷移成新資料庫。
- 網文章節掃描在背景進行；每輪翻譯完整驗證 Checkpoint 一次，後續保存不重讀既有分段正文。
  檔案屬性變更會中止本輪，要求重新驗證。
- 共用原子寫入使用同目錄唯一暫存檔、flush/fsync 與失敗清理；並序列化同程序內同目標的替換。

## 安裝與啟動

Windows、Python 3.12、uv：

```powershell
uv sync --group dev
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/setup_ocr.ps1
uv run python gui_main.py
```

OCR 獨立環境用於隔離 PyTorch 等依賴，不是另一個應用程式。
安裝腳本使用 `requirements/ocr-torch.txt` 與 `requirements/ocr.txt`；直接依賴有版本固定，
間接依賴尚未完整鎖定。模型首次使用可能下載；應用程式不傳送截圖或辨識文字。背景辨識使用 CPU。

## 程式與測試

```text
ui/app.py                       主視窗與平行模式管理
ui/webnovel/                    網文介面
ui/ebook/                       電子書介面
ui/shared/                      Prompt 編輯與共用元件
core/webnovel/                  網文流程
core/ebook/works.py             電子書作品
core/ebook/chapters.py          章節原文與譯文
core/ebook/exports.py           頁碼圖片輸出
core/ebook/ocr/                 裁切、定位、辨識與紀錄
core/shared/                    設定、路徑、翻譯支援與檔案寫入
tests/{webnovel,ebook,shared}/   合成資料回歸測試
```

```powershell
uv run python -m pytest tests/ebook -p no:cacheprovider
```

GitHub 僅提交程式、合成測試、文件與依賴清單。`outputs`、`checkpoints`、`runtime`、`cache`、
`private-tests`、`.local-ocr`、虛擬環境、私人圖片及譯文全部排除。
自訂輸出根目錄也必須排除，不能加入版本控制。

打包規格已更新背景程序路徑，**尚未驗證 EXE 成品**。EXE 仍需另備 `runtime/ocr/venv`；
既有 v0.4.0 發佈包不含此次變更。
