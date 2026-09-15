"""
memory 包 —— ChromaDB 向量记忆与文档检索(RAG 底座)

v1.6.0 起接入主流程(默认开启, 可经 .env MEMORY_ENABLED=false 关闭):
    - 任务级长期记忆: 调研完成后 save_run 保存, 新任务开始时检索相似历史复用素材;
    - 上传文档分块检索(RAG): save_document 分块向量化, 新任务按主题检索相关块注入;
    - 容错: chromadb 不可用 / 初始化失败时 get_memory_store() 返回 None, 主流程降级,
      不阻断任何功能。

模块索引:
    vector_memory.py   MemoryStore(写入/检索/管理) + chunk_text(分块) + get_memory_store(单例)
"""
