import networkx as nx
import json
import os

class MemoryGraph:
    def __init__(self, filepath="memory.json"):
        self.G = nx.DiGraph()
        self.filepath = filepath
        self.load() # 初始化时自动尝试读取本地数据

    # ==========================================
    # 1. 核心功能：添加节点 (带强校验)
    # ==========================================
    def add_memory(self, node_id: int, label: str, details: str, date: str):
        # 强制校验 ID 必须是整数
        if not isinstance(node_id, int):
            raise TypeError(f"节点 ID 必须是整数！你传进来的是 {type(node_id)}")
        
        # 强制校验必填项不能为空
        if not all([label, details, date]):
            raise ValueError("label, details, date 不能为空！")
            
        # 防止 ID 冲突覆盖
        if node_id in self.G.nodes:
            raise ValueError(f"节点 ID {node_id} 已存在，请使用新的 ID！")
            
        # 存入图库，并附带属性
        self.G.add_node(node_id, label=label, details=details, date=date)
        print(f"✅ 成功添加记忆节点 [{node_id}] - {label}")
        self.save() # 每次修改自动保存

    # ==========================================
    # 2. 核心功能：建立节点关联 (画线)
    # ==========================================
    def link_memories(self, source_id: int, target_id: int, relation: str):
        # 检查两边的节点是不是都真实存在
        if source_id not in self.G.nodes or target_id not in self.G.nodes:
            raise ValueError("源节点或目标节点不存在，无法建立关联！")
            
        # 添加有向边
        self.G.add_edge(source_id, target_id, relation=relation)
        print(f"🔗 成功建立关联: [{source_id}] --({relation})--> [{target_id}]")
        self.save()

    # ==========================================
    # 3. 辅助功能：获取当前最大 ID (供外部生成新 ID)
    # ==========================================
    def get_max_id(self) -> int:
        if len(self.G.nodes) == 0:
            return 0
        return max(self.G.nodes)

    # ==========================================
    # 4. 持久化：存入 JSON 文件
    # ==========================================
    def save(self):
        # 将 NetworkX 图转为字典
        data = nx.node_link_data(self.G)
        with open(self.filepath, "w", encoding="utf-8") as f:
            # ensure_ascii=False 保证中文正常显示，indent=2 让 JSON 好看
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ==========================================
    # 5. 持久化：从 JSON 恢复 (解决刚才说的坑)
    # ==========================================
    def load(self):
        if not os.path.exists(self.filepath):
            return # 文件不存在就直接返回，说明是第一次运行
            
        with open(self.filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            self.G = nx.node_link_graph(data)
            
            # 【核心修复】JSON 里的 Key 会变成字符串，我们把它转回整数
            mapping = {}
            for node in self.G.nodes:
                if isinstance(node, str) and node.isdigit():
                    mapping[node] = int(node)
            
            # 如果存在需要转换的 ID，重新映射节点
            if mapping:
                self.G = nx.relabel_nodes(self.G, mapping)
            print(f"📂 成功从 {self.filepath} 加载了 {len(self.G.nodes)} 个记忆节点。")
