# 程式整理與效能修正（2026-10-09）

## 本次變更

1. OCR 共用灰階、連通元件與特殊區塊分析結果；逐行偵測不再先執行舊網格推算。
2. 每行只更新權威紀錄 manifest；整理後 TXT 在工作結束時產生，GUI 不重複保存同份完成結果。
3. 圖片讀取、備份、定位、裁切、匯出及清除搬到背景。網文載入、手動重新檢查與批次翻譯前的章節掃描也在背景處理。
4. 匯出拆成準備、確認、提交，合併排序只做一次；確認期間索引改變時拒絕寫入。
5. OCR 模型載入／單行辨識加入 300／60 秒預設逾時；停止失去回應的自有程序，保留已完成與未嘗試行，支援續跑。
6. Checkpoint 使用單輪有效的寫入器，不反覆重讀所有舊分段正文；每次新執行仍完整驗證，檔案屬性改變時拒絕沿用記憶中的結果。
7. 統一原子文字寫入：唯一暫存檔、flush/fsync、替換失敗清理及同程序內的目標替換鎖。
8. 移除已無入口的固定分欄模式與過時提示；保留舊版面格式讀取。
9. 新增作品以商品 ID 直接查目標檔，不掃描所有作品。

另將電子書匯出移至 `ui/ebook/ocr_export.py`，網文輸出與錯誤對話框移至
`ui/webnovel/output_actions.py`。背景事件僅傳工作識別碼，Tk 回呼保留在主執行緒；
網文頁銷毀時取消其定時回呼。測試在主執行緒回收各測試建立的 Tk 視窗，
避免 Windows/Tk 在後續背景執行緒回收舊視窗時中止程序。

## 相容性與保留項目

- 未遷移作品或私人樣本，未變更原圖、校訂紀錄與章節編號規則。
- `chapters.json` 格式不變；仍是全作品索引，每次正式提交寫入一次，並非分章資料庫。
- 原始文字、校訂文字、逐行進度與失敗恢復保留。
- 不新增 LLM 電子書翻譯、不變更先前尚待實作的匯出對話框設計。
- 沒有提交或推送 GitHub；`outputs`、`runtime`、`private-tests` 維持忽略。

## 驗證限制

最終可用回歸集合：680 通過、1 跳過、1 排除、6 子測試通過；另有上述三份
OpenCV 相關測試檔未納入。Ruff 靜態檢查通過。Tk 多視窗測試仍偶見
`ThemeChanged` 銷毀警告，但最終未出現失敗或程序中止。

本機既有 `.venv/Lib/site-packages/cv2/cv2.pyd` 載入失敗，Windows 回報
WinError 4551：「應用程式控制原則已封鎖此檔案」。未修改或繞過安全設定。
因此實際 OpenCV 偵測與相關端到端測試尚未驗證，亦未重新量測真實 OCR 效能或驗證 EXE。
儲存／匯出與背景 UI 測試使用合成資料；其中不測偵測演算法的 UI 測試以測試替身隔離偵測器。

可用回歸集合：

```powershell
.venv/Scripts/python.exe -B -m pytest tests -q -s -p no:cacheprovider `
  --ignore=tests/ebook/test_ebook_autoframe.py `
  --ignore=tests/ebook/test_ebook_lines.py `
  --ignore=tests/ebook/test_ebook_tab_refresh.py `
  -k 'not capture_recognize_edit_reopen'
```

Windows 允許既有套件正常載入後，應取消上述排除條件重跑，並以本機私人樣本驗證定位結果。
