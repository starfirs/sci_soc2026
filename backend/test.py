from memory_graph import MemoryGraph

# 1. 实例化图库
my_brain = MemoryGraph()

# 2. 模拟外面的业务逻辑：生成一个新 ID 并添加节点
new_id_1 = my_brain.get_max_id() + 1
my_brain.add_memory(
    node_id=new_id_1, 
    label="洛必达法则", 
    details="今天高数课学了求极限的法则...", 
    date="2026-11-04"
)

# 3. 再加一个节点
new_id_2 = my_brain.get_max_id() + 1
my_brain.add_memory(
    node_id=new_id_2, 
    label="极限的定义", 
    details="微积分的基础概念...", 
    date="2026-11-01"
)

# 4. 让它们产生关联（注意这里用的是整数 ID）
my_brain.link_memories(source_id=new_id_1, target_id=new_id_2, relation="前置知识")

# 5. 打印看看结果
print("\n--- 图库内部状态 ---")
for node, data in my_brain.G.nodes(data=True):
    print(f"节点: {node} | 类型: {type(node)} | 标签: {data['label']}")

print("\n--- 看看连线 ---")
for u, v, data in my_brain.G.edges(data=True):
    print(f"{u} -> {v} ({data['relation']})")