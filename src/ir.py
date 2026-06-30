import hashlib
from dataclasses import dataclass, fields
from typing import Optional


@dataclass
class BaseNode:
    parent: Optional["BaseNode"]
    children: list["BaseNode"]
    code: str
    file_path: str

    def __hash__(self):
        return hash((self.code, self.file_path))

    def __eq__(self, other):
        return self.code == other.code

    def to_dict(self) -> dict:
        return {
            "type": self.__class__.__name__,
            "code": self.code,
            "file_path": self.file_path,
            "children": [child.to_dict() for child in self.children],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "BaseNode":
        node_type = data["type"]
        node_cls = globals().get(node_type, BaseNode)
        children = [BaseNode.from_dict(child) for child in data["children"]]

        cls_fields = {field.name for field in fields(node_cls)}
        cls_fields.remove("children")
        kwargs = {k: v for k, v in data.items() if k in cls_fields}
        kwargs["children"] = children
        kwargs["parent"] = None
        if "used_idents" in kwargs:
            kwargs["used_idents"] = set(IdentifierNode.from_dict(ident) for ident in data["used_idents"])
        node = node_cls(**kwargs)

        for child in node.children:
            child.parent = node
        return node


@dataclass(eq=False)
class ModuleNode(BaseNode):  # impl for Rust, defmodule for Elixir, etc.
    template: str

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "template": self.template,
        })
        return base_dict


@dataclass(eq=False)
class FunctionNode(BaseNode):
    name: str
    namespace: str
    position: tuple[int, int]  # (start_byte, end_byte)
    signature: str
    docstring: str
    code_with_context: str  # code including signature, docstring
    used_idents: set["IdentifierNode"]
    contexts: list[str] | None = None
    tests: set["TestFunctionNode"] | None = None

    def __hash__(self):
        return super().__hash__()

    def md5(self):
        return hashlib.md5(self.code.encode("utf-8") + self.file_path.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "name": self.name,
            "namespace": self.namespace,
            "position": self.position,
            "signature": self.signature,
            "docstring": self.docstring,
            "code_with_context": self.code_with_context,
            "used_idents": [ident.to_dict() for ident in sorted(self.used_idents, key=lambda x: x.code)]
        })
        return base_dict

    def to_benchmark_dict(self) -> dict:
        return {
            "name": self.name,
            "signature": self.signature,
            "docstring": self.docstring,
            "code": self.code,
            "file_path": self.file_path,
            "position": self.position,
            "code_with_context": self.code_with_context,
            "hash": self.md5(),
            "contexts": "\n".join(self.contexts) if self.contexts else "",
            "tests": [test.to_benchmark_dict() for test in
                      sorted(self.tests, key=lambda x: (x.name, x.code))] if self.tests else [],
        }


@dataclass(eq=False)
class TestFunctionNode(BaseNode):
    name: str
    namespace: str
    position: tuple[int, int]  # (start_byte, end_byte)

    def __hash__(self):
        return super().__hash__()

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "name": self.name,
            "namespace": self.namespace,
            "position": self.position,
        })
        return base_dict

    def to_benchmark_dict(self) -> dict:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "file_path": self.file_path,
            "position": self.position,
        }


@dataclass(eq=False)
class TestHelperFunctionNode(BaseNode):
    """
    Represents a helper function used within test functions.
    """
    name: str

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "name": self.name,
        })
        return base_dict


@dataclass(eq=False)
class CallNode(BaseNode):
    func_name: str
    namespace: str
    arguments: list[str]

    def __hash__(self):
        return super().__hash__()

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "func_name": self.func_name,
            "namespace": self.namespace,
            "arguments": self.arguments,
        })
        return base_dict


@dataclass(eq=False)
class ImportContextNode(BaseNode):
    """
    Represents an import statement within the repo.
    """
    use: str  # 导入名称，*或具体名称；注意这里是程序中实际使用的名称，即可能是别名
    path: str
    autoload: bool = False  # 是否为模拟自动加载而增加的导入语句

    def __hash__(self):
        return hash((self.code, self.file_path, self.use, self.path, self.autoload))  # 同code会被拆分为多个import节点

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "use": self.use,
            "path": self.path,
            "autoload": self.autoload,
        })
        return base_dict


@dataclass(eq=False)
class ImportExternalNode(BaseNode):
    """
    Represents an import statement for std or external libraries.
    """
    use: str
    module: str
    autoload: bool = False

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "use": self.use,
            "module": self.module,
            "autoload": self.autoload,
        })
        return base_dict


@dataclass(eq=False)
class IdentifierNode(BaseNode):
    """
    Represents an identifier usage in the code.
    """
    name: str
    namespace: str

    def __hash__(self):
        return super().__hash__()

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "name": self.name,
            "namespace": self.namespace
        })
        return base_dict

    def to_benchmark_dict(self) -> dict:
        return {
            "name": self.name,
            "namespace": self.namespace,
            "code": self.code,
        }


@dataclass(eq=False)
class ContextNode(BaseNode):
    """
    Represents a context code block, such as struct, enum, etc.
    """
    name: str
    context_type: str
    position: tuple[int, int]  # (start_byte, end_byte)

    def __hash__(self):
        return hash((self.code, self.file_path, self.name, self.context_type))

    def to_dict(self) -> dict:
        base_dict = super().to_dict()
        base_dict.update({
            "name": self.name,
            "context_type": self.context_type,
            "position": self.position,
        })
        return base_dict
