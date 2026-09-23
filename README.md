# 日文小說繁體中文翻譯器

以 Python 與 Tkinter 製作的 Windows GUI 工具，從日文小說網站讀取章節，透過 Gemini API 或本機 Codex 翻譯為繁體中文，並輸出可編輯的 TXT 與適合閱讀的 HTML。目前支援「成為小說家吧」與 Kakuyomu。

目前為私人開發版本。

![AutoTranslater 主畫面](docs/screenshots/main-window.png)

## 下載 Windows 版本

[下載 AutoTranslater v0.3.0 Windows ZIP](https://github.com/YungYiHsu/AutoTranslater/releases/download/v0.3.0/AutoTranslater-v0.3.0-win64.zip)

下載後請完整解壓縮，再執行 `AutoTranslater.exe`。ZIP 根目錄已直接包含程式檔，不會再多包一層 `AutoTranslater/`。私人儲存庫的附件需要先登入具有存取權限的 GitHub 帳號。

v0.3.0 包含以下功能；開發者工具頁僅原始碼版提供。更新時先關閉程式，替換 EXE 與整個 `_internal` 資料夾，保留原有 `outputs/`、`checkpoints/`、`prompts/`、`.env` 與 `config.json`，不要用 ZIP 的預設設定覆蓋個人設定。

## 目前功能

- 翻譯頁支援滑鼠滾輪與垂直捲動條，內容超出視窗時才顯示捲動條；底部操作按鈕與進度固定顯示。
- 設定及資訊區可摺疊；頁面捲動不干擾 Prompt 文字框、下拉選單及重試次數欄位的原有操作。

- GUI 可用按鈕切換「成為小說家吧」或 Kakuyomu，兩個網站各自使用專屬網址解析與本地作品清單。
- Kakuyomu 支援作品首頁與 episode 網址，可讀取作品名稱、作者、摘要、完整目錄及章節正文；介面與輸出檔名使用作品內的章節順序，不使用長篇 episode ID。
- 選擇的網站與貼入網址不一致時，GUI 會直接提示應切換到哪個網站按鈕。
- 啟動新版時會將可確認來源的舊版 Syosetu 作品移至 `outputs/Syosetu/`；同名衝突不會合併或覆蓋，而會顯示錯誤。
- 輸入作品首頁或章節網址；章節網址會自動拆分為作品與章節。
- 從本機作品下拉選單快速載入既有作品。
- 在 GUI 選擇 Gemini 3.5 Flash／Flash-Lite，或手動輸入其他 Model ID；自訂項目會永久加入下拉選單並寫入 `config.json`。
- 可在 GUI 設定每個 API 工作失敗後重試 0～10 次；開始前會顯示依目前 checkpoint 計算的實際請求上限。
- 模型選單旁顯示該模型「今日已請求」次數；切換模型或工作完成、失敗、取消時刷新。包含本程式的重試與失敗請求，僅統計本程式呼叫，並非 Google 官方剩餘額度。
- 讀取作品名稱、作者、摘要與章節目錄。
- 首次選擇作品時翻譯作品名稱及摘要，建立作品專屬資料夾與 `work.json`。
- 以數字選擇章節，並顯示下一個待翻譯章節。
- 分析章節後可分別選擇是否翻譯章節標題、翻譯正文，以及分析並更新專有名詞記憶；未翻譯的標題與正文會直接使用日文原文。
- 首次實際呼叫章節翻譯 API 時記住本次處理選項，作為下一章及重啟後的預設。
- 依自然段落切割長篇正文，顯示「翻譯第 i/n 個 chunk 中」。
- 「分析章節」旁可設定並保存每個 chunk 的原文字元上限；分析後可在章節資訊下方指定 chunk 數量重新分段，不重抓網頁、不呼叫 API。
- 章節名稱以獨立 API 工作翻譯；TXT 與 HTML 以中文作品名、中文章節名為主，並保留日文原名。
- 章節名稱、每個正文 chunk、每個 chunk 的專有名詞更新各自保存 checkpoint；中斷後只重做未成功的階段。
- 輸出固定命名的 `0001 - 作品名稱.txt` 與 `.html`。
- TXT 是可手動修正的正文來源；開啟 HTML 時若內容不同，會詢問是否以 TXT 覆蓋 HTML。
- HTML 提供上一章／下一章按鈕，連到已有完整 TXT／HTML 或手動翻譯草稿的章節；章節不連號也能使用。
- 選擇作品時會整理一次既有 HTML 導航；翻譯新章後只更新新章與相鄰章節，不會重掃全部檔案。
- GUI 內可編輯章節翻譯、作品資料翻譯與專有名詞整理 Prompt；程式必要格式獨立顯示且不可修改。
- 每部作品使用簡單的 `terms.json` 保存「日文原詞 → 繁體中文譯名」。
- 翻譯前自動找出本章已知名詞並加入每個 chunk 的完整 Prompt。
- 每個 chunk 翻譯後由 Gemini 分析新名詞並立即寫入 `terms.json`，再記錄成功標記；新增詞會供後續 chunk 使用，衝突時保留舊譯名。
- GUI 可手動執行「整理專有名詞」：完整詞庫依每批最多 500 組或 40,000 字元送交 Gemini，相鄰批次重疊 5 組；每批都會以「新增」與「移除」兩區預覽，可逐項勾選、略過或停止，正常翻譯不會自動整理。
- 手動執行「重新整理完整目錄」，取得作者改稿後的最新章節目錄。
- 作者修改作品名稱後仍以網站作品 ID 找回原資料夾，不會因新名稱建立第二份作品；同一 ID 出現多個資料夾時會停止並報錯。

目前支援 Gemini API 與 Codex。本地模型選項保留於 GUI，但尚未實作；Codex 使用雲端模型，不是本地離線推論，EXE 不內含 Codex 執行環境，需另行安裝並登入。

## 批次翻譯

批次翻譯：選擇作品後按「批次翻譯」，設定起訖章節與處理項目。預設跳過已完成章節；勾選重新翻譯後，會清除那些章節本次勾選項目的 checkpoint。開始前統一確認範圍及既有輸出覆蓋，未完成章節可沿用 checkpoint。每章依目前 chunk size 自動分析，顯示本章 API 請求上限（Codex 顯示回合數），逐章存檔並更新記憶與導航。遇錯停止整批；取消會等目前工作安全停止，保留已完成成果。批次結束顯示完成、跳過、失敗與尚未完成數量，不逐章開啟 HTML。

## 匯出 EPUB 並手動上傳 Kindle

1. 選擇作品，在「作品資訊」按「匯出 EPUB／上傳 Kindle」。
2. 起始與結束預設為本地 TXT 的最小與最大章節號碼（包含兩端），可手動調整；下方顯示本地範圍與完整輸出資料夾。沒有本地 TXT 時停用匯出。按「匯出並開啟網頁」後，進度依章節合成推進，完成儲存才到 100%。
3. EPUB 以本地 TXT 的作品名稱、章節名稱及正文製作，包含手動修正內容與章節目錄。缺少章節、TXT、必要欄位或正文空白時會提示修正；同名 EPUB 會詢問覆蓋。
4. 檔案儲存在作品資料夾，名稱如 `0001-0010 - 中文作品名.epub`。完成後自動開啟 [Send to Kindle](https://www.amazon.com/sendtokindle)，登入 Amazon 並手動選取 EPUB 上傳；視窗提供「開啟資料夾」與「開啟上傳網頁」。

這是本地檔案匯出，不呼叫翻譯模型，也不改動章節完成狀態、記憶或 TXT／HTML。支援 Syosetu 與 Kakuyomu 的章節序號及舊版 TXT 檔名。尚未補譯的空白文件須先填入正文。此功能以目前原始碼版本為準。

## Codex 模式

1. 本機需安裝可執行 `codex app-server` 的 Codex CLI 或桌面版。程式會先從 PATH 尋找，Windows 也會尋找桌面版內附的執行檔。
2. GUI 首次切換為「Codex」時，自動在背景檢查登入並讀取本機 Codex 模型清單；也可按「更新模型／檢查 Codex 登入」重新載入。沿用目前 Windows 使用者與 `CODEX_HOME` 的登入狀態；未登入時可按「登入 Codex」，依瀏覽器提示完成 ChatGPT 登入。不會讀取、複製或保存登入憑證到本專案，也不會自動改用 API Key。清單取得失敗會顯示錯誤，不自行改用 Gemini。
3. Codex 模型只能從本機回報的下拉清單選擇，不支援手動輸入；可按「更新模型／檢查 Codex 登入」重新取得清單。模型選單不提供 `default`；載入清單後，若沒有已儲存的有效模型，就選清單第一個。選擇獨立保存於 `config.json` 的 `codex_model`，下次沿用，不影響 Gemini 設定。舊版 `default` 設定會在載入清單後轉為具體模型，不刪除既有對話或 checkpoint。
4. 選擇作品、分析章節、勾選標題／正文，再開始翻譯。沒有有效作品記憶時，選擇作品也會翻譯作品名稱和摘要並消耗額度；有有效記憶就直接沿用。章節分析及登入檢查不會送出模型請求。

Codex 模型欄下方可選擇推理強度，例如 `low`（低）、`medium`（中）、`high`（高）；選項依模型回報的支援範圍顯示，不提供 `default`。未設定過的模型或舊設定為 `default` 時，優先使用 `low`；不支援時採模型回報的預設強度，若預設值也不在支援清單中則採第一個支援值。尚未取得清單或自訂模型不在清單時，停用推理選單並要求更新清單，不猜測強度或送出翻譯。

推理強度依模型自動存入 `config.json` 的 `codex_model_efforts`，切換模型時還原各自上次設定；`codex_reasoning_effort` 保存目前選用值供請求使用。舊版單一強度會保留給當時選用的具體模型，不套用到所有模型。設定適用於新的作品名稱／摘要、章節標題與正文請求。若模型不再支援已存強度，改用 `low` 或該模型的預設強度；改變模型或強度後須重新分析章節，明確指定不同強度的 checkpoint 不會混用。既有有效作品名稱／摘要記憶仍照原規則沿用，不因切換強度自動重翻。此設定僅用於 Codex，「Gemini API」模式不受影響。

各模式不適用的控制項會隱藏：Gemini API 隱藏 Codex 登入、更新模型與推理強度；Codex 隱藏失敗重試、名詞更新勾選框與整理名詞按鈕；本地模式隱藏 Codex 專用控制項、失敗重試與開始翻譯。因尚未選作品、尚未分析或正在執行而暫時停用的控制項，仍保留顯示。

每部作品使用自己的 Codex 任務，作品資料、標題和各個 chunk 依序加入同一對話。仍使用 GUI 裡的章節／作品 Prompt，保留既有 TXT／HTML、標題與 chunk checkpoint。Codex 與 Gemini 的 checkpoint 分開；最終輸出路徑相同，已有輸出時仍需確認覆蓋。

Codex 不額外呼叫新名詞分析，也不改寫 `terms.json`；已有的匹配譯名仍會加入 Prompt。「整理專有名詞」在此模式停用，可切回 API 使用。對話上下文及 Codex 自動壓縮有助於延續翻譯，但不等同精確詞庫；切換模型或回頭翻譯舊章仍沿用同一作品對話，目前不自動分支，也不自動把手改 TXT 回饋到對話。

使用本機 ChatGPT 登入對應的 Codex 額度，不計入本程式的 Gemini 當日請求計數。開始前顯示的是應用層回合數，不是底層模型請求或額度的保證上限；歷史、推理與壓縮都可能影響消耗。登入及計費方式可參考 [Codex 官方登入說明](https://learn.chatgpt.com/docs/auth)。

### Codex 用量與手動壓縮

用量顯示為獨立區塊，額度採剩餘百分比，窗口依實際分鐘數換算為天、小時與分鐘。任務名稱顯示章節序號及標題／內文，不顯示內部識別碼。

批次視窗預設為 650 × 546，內容可捲動且操作按鈕固定於底部；「最小化程式」可將主視窗與批次視窗一同最小化。進行中顯示上一步消耗，結束時顯示標題／內文平均及本次總消耗（含有回報的失敗請求）；總計只包含標題與內文，不包含手動壓縮。缺失數據會註明，不當作零。

Codex 用量區另顯示「最近輸入上下文估計」：使用最近回報的輸入 Token 數及上下文容量計算比例，不使用累積消耗。這是最近一次輸入的估計，不包含其後新增的回覆，也不代表實際記住的內容比例。切換作品、壓縮後或缺少有效回報時顯示無法取得，待下一次翻譯回報再更新；不會為測量額外呼叫模型。

選擇作品後，「Codex 對話與用量」顯示帳號額度窗口、重置時間，以及上一個已結束步驟的 Token 用量。單章結束可查看本次逐步用量；批次結束顯示每次標題與每個正文 chunk 的平均，有用量的失敗請求也納入，未取得的數據與 checkpoint 重用不算零。

每部作品的 `codex_usage.jsonl` 以固定檔名追加請求開始及結束紀錄，包含模型、步驟、回合識別、Token 分類與額度前後快照；相同請求 ID 的後續事件是更新，不應重複加總。無法確認的回合標為 unknown，恢復時不額外發送翻譯請求；無法重建的用量留空。Token 由回合通知的累積差額取得，不把最後一次模型呼叫誤當整個回合，也不把 Token 換算成 Plus 百分比。額度差值只代表相同重置窗口內的帳號變化，可能包含其他任務。

監控不新增模型生成請求：接收現有 Token 事件，每次真正發送前後各查一次帳號額度，單次查詢最多等待 3 秒、不重試；另外可手動刷新及在選擇作品後刷新。查詢會增加連線流量與等待時間，並非宣稱服務保證零計費。紀錄只在步驟邊界寫入，不逐 Token 寫檔。寫入失敗會提示，不中斷翻譯。

「壓縮對話記憶」只在作品已有 Codex 任務且沒有工作執行時啟用，需確認後才使用正式壓縮介面。壓縮可能消耗額度，獨立記錄且不納入標題／正文平均；不變更翻譯成果或清除 checkpoint。斷線時保留待確認狀態，再按壓縮先查原回合，不能確定時不重送。若請求連回合 ID 都未取得，需先在 Codex 檢查原任務，不要直接反覆重試或刪紀錄。

### Codex 中斷處理

- 程式不自動重送 Codex 請求，API 重試設定在此模式停用。取消會在目前工作完成後停止，不保證立即中斷正在進行的回合。
- `codex_session.json` 記錄任務 ID、目前回合與最近一次回覆，不含登入憑證。重新執行時先查原回合；已完成就取回結果，不因斷線再次送出。實際對話歷史由本機 Codex 保存，單獨搬移 outputs 不保證能在另一部電腦接續。
- 已確認失敗或無效內容後，可手動再次執行。無法確認請求有沒有送達時會停止並提示，不會自行建立另一個任務。請先檢查該任務，勿直接刪除紀錄來繞過確認。
- `codex_session.lock` 防止兩個程式同時操作同一作品；正常結束會移除。若程式異常退出，必須先確認沒有其他翻譯在執行，才手動刪除這個鎖檔。
- 翻譯連線限制為唯讀、停用 shell／網頁搜尋／已設定的外部整合，並拒絕工具核准要求；不修改使用者本機 Codex 設定。

## 環境需求

- Windows 10 或 Windows 11
- Python 3.12
- [uv](https://docs.astral.sh/uv/)
- Gemini API Key（API 模式），或已以 ChatGPT 登入的本機 Codex（Codex 模式）

## 建立環境

在專案根目錄執行：

```powershell
uv sync --dev
Copy-Item .env.example .env
```

編輯 `.env`：

```dotenv
GEMINI_API_KEY="你的 API Key"
```

一般設定位於 `config.json`：

```json
{
  "model": "gemini-3.5-flash",
  "saved_models": [],
  "output_directory": "outputs",
  "chunk_size": 4000,
  "retry_attempts": 0,
  "translate_title": true,
  "translate_body": true,
  "update_terms": true
}
```

## 啟動 GUI

不顯示終端視窗：

```powershell
Start-Process .\.venv\Scripts\pythonw.exe -ArgumentList 'gui_main.py' -WorkingDirectory (Get-Location) -WindowStyle Hidden
```

開發除錯時可改用：

```powershell
uv run python gui_main.py
```

## Windows 可攜版

正式發布包採用 PyInstaller `onedir` 模式。請先將 ZIP 完整解壓縮到具有寫入權限的資料夾，再執行 `AutoTranslater.exe`。請勿直接從 ZIP 內執行，也不建議放在 `C:\Program Files`。

### 使用發布 ZIP

1. 從私人 GitHub 儲存庫的 Releases 下載 `AutoTranslater-v0.2.0-win64.zip`。
2. 將 ZIP 完整解壓縮到自己的文件或工具資料夾。
3. 保留 `AutoTranslater.exe`、`config.json` 與 `_internal/` 的相對位置。
4. 執行 `AutoTranslater.exe`；不需要另外安裝 Python。

### API Key 與免費額度

第一次使用 Gemini 功能時，GUI 會要求輸入 Gemini API Key，並將它保存在 EXE 同層的 `.env`。請勿分享、上傳或將含有 `.env` 的資料夾交給他人。

Gemini 免費方案有請求次數限制。額度用完時程式會顯示專用訊息；請等待額度重置，或前往 Google AI Studio 查看用量與方案。

### Windows SmartScreen

目前 Windows 版本尚未進行程式碼簽章，因此第一次啟動時可能出現 SmartScreen 警告。請只執行從此私人儲存庫下載，且 SHA-256 與發布頁一致的檔案。

程式會在 EXE 同層讀取 `config.json`，並建立下列使用者資料：

- `.env`
- `prompts/`
- `outputs/`
- `checkpoints/`
- `logs/`

缺少 `config.json` 時，程式會建立模型名稱為空白的設定並在使用 Gemini 時報錯；正式發布 ZIP 因此必須保留 EXE 同層的 `config.json`。

開發者可在專案根目錄執行：

```powershell
.\scripts\build_windows.ps1
```

建置腳本使用 `AutoTranslater.spec`，將內建 `resources/` 收入程式，並把外部 `config.json` 複製到：

```text
dist/AutoTranslater/
├── AutoTranslater.exe
├── config.json
└── _internal/
```

### 驗證 SHA-256

在 ZIP 所在資料夾執行：

```powershell
Get-FileHash .\AutoTranslater-v0.2.0-win64.zip -Algorithm SHA256
```

將輸出的 Hash 與 GitHub Release 公布的值逐字比對。SHA-256 可確認下載檔案與發布版本相同，但不能取代程式碼簽章。

目前 `v0.2.0` Windows ZIP 的 SHA-256：

```text
0459B44186512893C799DC766AF30269DF68CAA3D3E7E352147FC133C28FB497
```

## 使用流程

1. 在作品欄貼上作品首頁／章節網址，或選擇本機作品。
2. 按「選擇作品」，等待作品資料與摘要完成。
3. 輸入章節數字並按「分析章節」。分析只會擷取、切塊與檢查 checkpoint，不呼叫 Gemini。
4. 在「本章處理項目」選擇是否翻譯章節標題、翻譯內文及更新專有名詞記憶，再按「開始翻譯」。取消內文翻譯時會直接輸出日文原文，並停用新專有名詞分析。
5. 完成後可開啟 HTML 或輸出資料夾。

### 調整分段大小與數量

在「章節選擇」的「Chunk 字元上限」輸入正整數，按 Enter、離開欄位或分析章節時保存至 `config.json`。首次分析依此上限切割；它是原文字元數，不是 Token 數，也不包含 Prompt 或專有名詞。修改上限後須重新分析章節。

分析完成後，「章節資訊」下方的「Chunk 數量」會填入目前數量。手動指定範圍為 1～20；修改後按旁邊的「重新分析」，會使用已取得的原文，優先沿段落、換行或句尾分成指定數量，盡量平均分配。自然分段點不足時會提示減少數量，原分析結果仍保留。此限制不影響依字元上限自動切割的數量。

指定數量只適用於當前章節，優先於字元上限，因此單段可能超過原上限；不會改寫全域大小設定。翻譯期間不能修改這些欄位。重新分段會重查 checkpoint 並更新請求上限；分段不同時不沿用原任務的標題、正文及名詞 checkpoint，但不刪除舊紀錄或既有輸出。

### 翻譯選項與預設記憶

首次使用時三項處理選項全部開啟。章節翻譯首次實際呼叫 API 時，會將當次生效的選項保存至 `config.json`，供下一章與重啟後使用；不依作品或模型區分，套用後仍可手動調整。

- 僅調整勾選、分析章節、開始前檢查失敗，或完全使用 checkpoint 而未呼叫 API：不更新預設。
- 已呼叫 API，之後翻譯失敗或取消：仍保留當次選項。
- 不翻譯內文時，專有名詞更新會停用，保存的選項也會是關閉。
- 舊設定檔未包含這三個欄位時，仍以全部開啟作為初始預設。

取消標題翻譯不會建立標題 checkpoint；取消內文翻譯不會建立正文 checkpoint；取消專有名詞分析不會建立新的 term checkpoint。既有 checkpoint 與 `terms.json` 都會保留，之後重新勾選時仍可繼續使用。若三項 API 工作都未選擇，程式會直接輸出日文標題與正文，不要求 API Key，也不消耗 API 額度。

若作品或章節不存在，程式會顯示錯誤。既有輸出、手動修改過的摘要或 TXT/HTML 衝突，會先要求使用者確認，不會另外產生重複檔案。

### API 阻擋後改用其他翻譯軟體

Gemini 成功回應但沒有提供有效內容時（例如輸入被內容政策阻擋），錯誤視窗會提供「查看 LLM 輸出」與「建立空白文件」。手動補譯流程如下：

1. 按「建立空白文件」。程式會以本章既定檔名同時建立 TXT 與 HTML，並保留作品、章節及來源資料。
2. 這組文件會先標記為「部分完成」，但空白 HTML 仍會加入上一章／下一章導覽。
3. 開啟 TXT，把其他翻譯軟體產生的譯文貼在 `========================================` 分隔線下方。
4. 如需留下紀錄，也可以修改 TXT 上方的 metadata。例如將翻譯引擎改成 `翻譯引擎：其他軟體 / 手動翻譯（手動）`。
5. 回到 GUI 按「開啟 HTML」。偵測到 TXT 與 HTML 不同時，選擇以 TXT 覆蓋 HTML。
6. 程式會用 TXT 的正文與 metadata 重新產生 HTML，並移除空白草稿標記。若章節目前不是「已完成」，開啟 HTML 時會詢問是否更新章節狀態；同意後會檢查輸出，符合完成條件才更新為「已完成」。

同步至 HTML 的 metadata 包括：`作品：`、`章節：`、`日文作品名：`、`日文章節名：`、`來源：`及`翻譯引擎：`。請保留這些欄位名稱與正文分隔線。同步檔案與更新章節狀態是分開的確認操作；不同意更新狀態時，仍可開啟 HTML 閱讀。

目前程式以正文內容判斷 TXT 與 HTML 是否不同，因此只修改 metadata 不會觸發同步。若只想更新 metadata，可先在 TXT 正文加入一段非空白的暫時文字，再按「開啟 HTML」同步一次；接著將正文恢復為原本內容，再同步一次，即可在保留原正文的情況下更新 HTML metadata。正文在同步時不可為空白。

## 資料位置

`logs/api_usage.json` 依 Gemini 的美國太平洋時間午夜重置規則，保存各模型本額度日的請求次數，重啟 GUI 後保留。臺灣時間夏令期間下午 3 點、冬令期間下午 4 點重置，GUI 每 30 秒檢查一次。升級時保留仍有效的舊計數，但舊資料未記錄每次請求時間，無法精確回推新額度日的歷史用量。首次使用從零開始。使用 checkpoint 或直接輸出日文原文不增加次數；本地與 Codex 模式會隱藏計數。

原始碼 GUI 另有「開發者工具」分頁，可調整四類 Gemini `safety_settings`。選擇後按「套用」，後續作品名稱與摘要、章節標題、正文、專有名詞分析及手動整理請求都使用該設定。預設不指定門檻；可選 `OFF`、`BLOCK_NONE` 及三種風險機率門檻。設定僅保留於本次執行，工作執行中不能更改；不影響 Codex，不清除 checkpoint，也不自動重新翻譯。此設定無法關閉服務端核心保護。打包 EXE 排除開發者模組並維持模型預設。

```text
outputs/
├── Syosetu/
│   └── <作品名稱>/
│       ├── work.json
│       ├── chapters.json
│       ├── terms.json
│       ├── synopsis.txt
│       ├── 0001 - 作品名稱.txt
│       └── 0001 - 作品名稱.html
└── Kakuyomu/
    └── <作品名稱>/

checkpoints/
└── <工作識別碼>/
    ├── metadata.json
    ├── title.json
    ├── chunks/
    │   └── 0000.json
    └── terms/
        └── 0000.done.json

prompts/
├── translation.txt
├── work_metadata_translation.txt
└── term_organization.txt
```

舊版直接位於 `outputs/<作品名稱>/` 的資料，只會在 `work.json` 能確認為 Syosetu 作品時自動搬移。無法辨識的資料夾會保持原狀。

舊版 Syosetu `work.json` 即使沒有新版的 `site` 與 `work_id` 欄位仍可直接載入；單純選擇或閱讀舊作品不會改寫檔案。TXT、HTML、`terms.json`、舊檔名及舊 checkpoint 也會繼續沿用既有相容流程，只在使用者觸發原本就會儲存的操作時更新相關檔案。

`resources/prompts/` 保存程式預設 Prompt；`prompts/` 是使用者可修改的副本，兩者用途不同。

章節 Prompt 支援 `{Novel_Content}` 與 `{Term_Memory}`。GUI 保留標記不展開；API 請求送出前才分別替換成目前 chunk 與本章匹配到的既有名詞。標記缺少時內容會自動附加在最後，重複標記則拒絕儲存或翻譯。

專有名詞內容只以作品資料夾內的 `terms.json` 為準；term checkpoint 只記錄該 chunk 是否已成功更新，不重複保存名詞內容。每個 chunk 的名詞分析只會收到該 chunk 的原文、譯文，以及當時已套用的正式與暫時名詞。舊版單一 JSON checkpoint 會在首次讀取時自動搬移，舊資料不會被視為已完成專有名詞更新。

## 品質檢查

```powershell
uv run ruff check .
uv run pytest -p no:cacheprovider --basetemp .codex-test-tmp
```

## 專案架構

```text
core/          核心流程、設定、切塊、checkpoint 與作品記憶
extractors/    Syosetu 與 Kakuyomu 作品及章節擷取
translators/   Gemini 翻譯策略
formatters/    TXT 與 HTML 輸出
ui/            Tkinter GUI 與背景工作執行緒
resources/     內建 Prompt 與 HTML 模板
tests/         自動化測試
gui_main.py    GUI 入口
```

## 尚未完成

- 本地模型翻譯

## 授權狀態

本專案目前未提供開源授權，保留所有權利。未經作者明確書面許可，不得商業使用、修改或重新散布。
