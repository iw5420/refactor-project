import sys
from pathlib import Path

# 讓測試無論從哪個 cwd 執行 pytest，都能 import repo 根目錄下的
# refactor_harness／graph 套件（程式碼內部大量用相對路徑讀取
# config/harness.yaml、fixtures/seed.sql，因此測試仍須從 repo 根目錄執行）。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
