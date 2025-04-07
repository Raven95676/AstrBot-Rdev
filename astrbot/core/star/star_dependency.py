import re
from functools import lru_cache
from typing import Callable, Dict, List, Optional, Tuple

import networkx as nx

from astrbot.core import logger
from astrbot.core.star.star import DependencyInfo, StarMetadata, star_registry


class DependencyAnalysis:
    """
    分析插件依赖并给出加载顺序
    """

    _VERSION_REQ_REGEX = re.compile(r"^\s*(>=|<=|>|<|=)?\s*(v)?([\d.]+|\*)\s*$")
    _VERSION_OPERATORS: Dict[str, Callable[[int], bool]] = {
        "=": lambda c: c == 0,
        ">": lambda c: c > 0,
        ">=": lambda c: c >= 0,
        "<": lambda c: c < 0,
        "<=": lambda c: c <= 0,
        "*": lambda _: True,
    }

    def __init__(self) -> None:
        """初始化依赖分析器"""
        self._graph = nx.DiGraph()
        self._cached_order = None

    @staticmethod
    @lru_cache(maxsize=256)
    def _parse_version(version_str: str) -> Optional[Tuple[int, ...]]:
        """解析版本字符串为整数元组"""
        if not version_str:
            return None

        version_str = version_str.lstrip("v").strip()
        try:
            parts = tuple(int(part) for part in version_str.split(".") if part)
            if not parts:
                return None
            if any(p < 0 for p in parts):
                logger.debug(f"版本 '{version_str}' 包含负数，无效。")
                return None
            return parts
        except ValueError:
            logger.debug(f"版本 '{version_str}' 格式无效，包含非数字部分。")
            return None

    @staticmethod
    @lru_cache(maxsize=256)
    def _parse_req(
        version_req_str: str,
    ) -> Optional[Tuple[str, Tuple[int, ...]]]:
        """解析版本要求字符串"""
        if version_req_str.strip() == "*":
            return ("*", ())
        match = DependencyAnalysis._VERSION_REQ_REGEX.match(version_req_str)
        if not match:
            logger.warning(f"版本要求 '{version_req_str}' 格式无效。")
            return None
        op, _, version_str = match.groups()
        operator = op or "="
        version_parts = DependencyAnalysis._parse_version(version_str)
        if version_parts is None:
            logger.warning(f"版本要求 '{version_req_str}' 中的版本号无效。")
            return None
        return (operator, version_parts)

    @staticmethod
    def _check_version_req(
        req: Tuple[str, Tuple[int, ...]], actual_version: Tuple[int, ...]
    ) -> bool:
        """检查实际版本是否满足要求"""
        operator, req_version = req
        if operator == "*":
            return True
        comparison = DependencyAnalysis._compare_versions(actual_version, req_version)
        return DependencyAnalysis._VERSION_OPERATORS[operator](comparison)

    @staticmethod
    def _compare_versions(v1_parts: Tuple[int, ...], v2_parts: Tuple[int, ...]) -> int:
        """比较两个版本元组"""
        max_len = max(len(v1_parts), len(v2_parts))
        v1_padded = v1_parts + (0,) * (max_len - len(v1_parts))
        v2_padded = v2_parts + (0,) * (max_len - len(v2_parts))
        for p1, p2 in zip(v1_padded, v2_padded):
            if p1 != p2:
                return 1 if p1 > p2 else -1
        return 0

    def _validate_dependency(
        self,
        plugin: StarMetadata,
        dep_info: DependencyInfo,
    ) -> Optional[Tuple[str, str]]:
        """验证单个依赖项"""
        dep_name: str = dep_info.name
        optional: bool = dep_info.optional
        version_req_str: str = dep_info.version

        target_plugin = next((p for p in star_registry if p.name == dep_name), None)
        if not target_plugin:
            msg = f"插件 '{plugin.name}' 依赖 '{dep_name}'，但未找到该插件。"
            if not optional:
                raise ValueError(msg)
            logger.debug(f"{msg} 但因为是可选依赖，已忽略")
            return None

        if not target_plugin.activated:
            msg = f"插件 '{plugin.name}' 依赖 '{dep_name}'，但该插件未被激活。"
            if not optional:
                raise ValueError(msg)
            logger.debug(f"{msg} 但因为是可选依赖，已忽略")
            return None

        if version_req_str:
            requirement = self._parse_req(version_req_str)
            actual_version = self._parse_version(target_plugin.version)
            if requirement is None or actual_version is None:
                msg = f"插件 '{plugin.name}' 对依赖 '{dep_name}' 的版本要求 '{version_req_str}' 无效（实际版本：'{target_plugin.version}'）。"
                if not optional:
                    raise ValueError(msg)
                logger.debug(f"{msg} 但因为是可选依赖，已忽略")
                return None
            if not self._check_version_req(requirement, actual_version):
                msg = f"插件 '{plugin.name}' 要求 '{dep_name}' 版本 '{version_req_str}'，但实际版本为 '{target_plugin.version}'。"
                if not optional:
                    raise ValueError(msg)
                logger.debug(f"{msg} 但因为是可选依赖，已忽略")
                return None

        return (dep_name, plugin.name)

    def build_graph(self, plugins: List[StarMetadata]):
        """
        构建插件依赖图，找出加载顺序并检测循环依赖

        Args:
            plugins: 插件元数据列表
        """
        self._graph = nx.DiGraph()
        self._cached_order = None

        active_plugins = {p.name: p for p in plugins if p.activated}

        self._graph.add_nodes_from(active_plugins.keys())

        edges = set()

        for plugin in active_plugins.values():
            if not plugin.dependencies:
                continue

            for dep_info in plugin.dependencies:
                edge = self._validate_dependency(plugin, dep_info)
                if edge:
                    edges.add(edge)

        self._graph.add_edges_from(edges)

        try:
            if not nx.is_directed_acyclic_graph(self._graph):
                cycles = list(nx.simple_cycles(self._graph))
                cycle_str = "; ".join(" -> ".join(cycle) for cycle in cycles)
                raise ValueError(f"插件存在循环依赖，无法确定加载顺序: {cycle_str}")

            self._cached_order = list(nx.topological_sort(self._graph))
        except nx.NetworkXUnfeasible:
            raise ValueError("计算拓扑排序时出现意外错误")

    def get_load_order(self) -> List[str]:
        """
        获取插件的加载顺序

        Returns:
            List[str]: 插件的加载顺序列表
        """
        if self._cached_order is None:
            if not self._graph:
                return []

            if not nx.is_directed_acyclic_graph(self._graph):
                cycles = list(nx.simple_cycles(self._graph))
                cycle_str = "; ".join(" -> ".join(cycle) for cycle in cycles)
                raise ValueError(f"插件存在循环依赖，无法确定加载顺序: {cycle_str}")

            self._cached_order = list(nx.topological_sort(self._graph))

        return self._cached_order

    def get_plugin_deps(self, plugin_name: str) -> List[str]:
        """
        获取指定插件的所有相关依赖（依赖在前，插件在后）

        Args:
            plugin_name: 插件名称

        Returns:
            List[str]: 依赖插件列表，按加载顺序排序
        """
        if not self._graph.has_node(plugin_name):
            raise ValueError(f"插件 '{plugin_name}' 不存在于依赖图中")

        dependencies = nx.ancestors(self._graph, plugin_name)

        all_nodes = set(dependencies)
        all_nodes.add(plugin_name)

        load_order = self.get_load_order()

        ordered_deps = [node for node in load_order if node in all_nodes]

        return ordered_deps
