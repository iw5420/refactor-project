給 `fill_mode: "file_upload"` 的人工填值模板選用的檔案，見
`docs/03a_spec_collection_agent_architecture.md` 三章「檔案上傳」。

- `sample_image.png`：最小合法 1x1 透明 PNG（68 bytes），純占位、不含任何
  真實內容。

**不會被自動套用**——模板產生時 `file_paths` 一律留空，是否要用這個檔案、
還是指向真實素材，由人工依實際業務邏輯決定（如果 Java 端對檔案內容有
OCR、語音辨識這類實質判斷，用空白假檔案可能讓兩邊都因為「內容辨識不出
東西」走到同一種降級分支，產生沒有意義的驗證結果，見 03a 三章說明）。
