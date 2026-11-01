"""组装聚宽单文件的公共工具，不包含任何研究假设或数据。"""
import ast
import builtins

def definitions(path, names=None):
    source = path.read_text(encoding='utf-8')
    blocks = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and (names is None or node.name in names):
            blocks.append(ast.get_source_segment(source, node))
        elif names is None and isinstance(node, ast.Assign):
            blocks.append(ast.get_source_segment(source, node))
    return '\n\n'.join(blocks)

def protect_builtins(source):
    """只限定真正的内置名称引用，保留注释/格式，不触碰np.sum等属性。

    聚宽可能在执行用户代码前注入numpy名字；仅避免通配导入仍不够。
    在组装边界统一处理工具、研究核心和适配层，源模块仍保留正常Python语义。
    """
    lines = source.splitlines(keepends=True)
    references = [node for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                  and node.id in vars(builtins)]
    for node in sorted(references, key=lambda item: (item.lineno, item.col_offset), reverse=True):
        index = node.lineno - 1
        # AST列偏移按UTF-8字节计数；源码含中文，不能直接当作字符索引。
        column = len(lines[index].encode('utf-8')[:node.col_offset].decode('utf-8'))
        lines[index] = (lines[index][:column] + '_python_builtins.' + node.id
                        + lines[index][column + len(node.id):])
    return ''.join(lines)
