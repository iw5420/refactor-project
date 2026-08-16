"""④ 骨架實作 Agent node，對應 08a 十二章「與既有程式碼的介面異動」。"""
import asyncio

from scaffold_agent.types import BuildDbModelsResult


class TestScaffoldNodeRun:
    def test_merges_skipped_db_models_from_both_sources(self, monkeypatch):
        import graph.nodes.scaffold_node as scaffold_node

        async def fake_build_db_models(*, java_project_path, module_list):
            return BuildDbModelsResult(
                db_models={"app/models/hr.py": "class User(Base):\n    pass\n"},
                skipped_entities=[{"file_path": "Address.java", "class_name": "Address", "error": "@Embeddable"}],
            )

        def sync_fake_build_db_models(*, java_project_path, module_list):
            return asyncio.run(fake_build_db_models(java_project_path=java_project_path, module_list=module_list))

        monkeypatch.setattr(scaffold_node, "build_db_models", sync_fake_build_db_models)

        async def fake_generate_scaffold(*, python_project_path, python_structure, db_models):
            assert db_models == {"app/models/hr.py": "class User(Base):\n    pass\n"}
            return {
                "success": True,
                "error": None,
                "skipped_interfaces": [{"file_path": "x.py", "class_name": None, "function_name": "f", "error": "bad"}],
                "skipped_db_models": [{"file_path": "app/models/broken.py", "error": "syntax"}],
            }

        monkeypatch.setattr(scaffold_node.translator_cli, "generate_scaffold", fake_generate_scaffold)

        state = {
            "java_project_path": "/java",
            "python_project_path": "/python",
            "python_structure": {"directory_tree": "", "interfaces": []},
            "module_list": [],
        }
        result = asyncio.run(scaffold_node.run(state))

        assert result["scaffold_done"] is True
        assert result["skipped_interfaces"] == [
            {"file_path": "x.py", "class_name": None, "function_name": "f", "error": "bad"}
        ]
        # 兩個不同粒度來源合併成同一份固定 schema：檔案級（class_name
        # 補上 None）＋ entity 級（原樣併入），見 08a 十二章。
        assert result["skipped_db_models"] == [
            {"file_path": "app/models/broken.py", "error": "syntax", "class_name": None},
            {"file_path": "Address.java", "class_name": "Address", "error": "@Embeddable"},
        ]
        # 平行分支 node：只回傳自己的 key，不展開 state（見 01 五章）。
        assert set(result.keys()) == {"scaffold_done", "skipped_interfaces", "skipped_db_models"}

    def test_no_skips_does_not_warn(self, monkeypatch, caplog):
        import logging

        import graph.nodes.scaffold_node as scaffold_node

        def sync_fake_build_db_models(*, java_project_path, module_list):
            return BuildDbModelsResult(db_models={}, skipped_entities=[])

        async def fake_generate_scaffold(*, python_project_path, python_structure, db_models):
            return {"success": True, "error": None, "skipped_interfaces": [], "skipped_db_models": []}

        monkeypatch.setattr(scaffold_node, "build_db_models", sync_fake_build_db_models)
        monkeypatch.setattr(scaffold_node.translator_cli, "generate_scaffold", fake_generate_scaffold)

        state = {
            "java_project_path": "/java",
            "python_project_path": "/python",
            "python_structure": {"directory_tree": "", "interfaces": []},
            "module_list": [],
        }
        with caplog.at_level(logging.WARNING):
            result = asyncio.run(scaffold_node.run(state))

        assert result["skipped_db_models"] == []
        assert not any("被跳過" in r.message for r in caplog.records)
