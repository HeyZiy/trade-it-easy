# -*- coding: utf-8 -*-
"""
妙想 API 服务模块

基于东方财富妙想平台 API（mx-skills-suite）构建，提供以下功能：
1. 金融数据查询（实时行情、财务指标、股东结构等）
2. 金融资讯搜索（新闻、公告、研报、政策等）
3. 智能选股（条件筛选、板块成分股查询、股票推荐）
4. 自选股管理（查询、添加、删除）
5. 模拟组合管理（持仓、买卖、撤单、委托、资金查询）

使用方式：
    from src.mx.service import MXService

    service = MXService()

    # 金融数据查询
    result = service.query_financial_data("贵州茅台最新价")

    # 资讯搜索
    result = service.search_news("格力电器最新研报")

    # 智能选股（消费入口：类型化名单）
    candidates = service.screen_codes("今日涨幅2%的股票")   # [(code, name), ...]
    rows, total = service.screen_stocks("今日涨幅2%的股票")  # 低层原始行（MX schema 细节）

    # 自选股管理
    codes, names = service.fetch_self_selected()
    service.add_self_select("贵州茅台")
    service.remove_stocks(["002284", "600775"])

    # 模拟交易
    service.query_positions()
    service.buy_stock("600519", 100, 1780.0)
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import requests

from data_provider.codes import normalize_stock_code
from src.config import get_config

logger = logging.getLogger(__name__)


class MXService:
    """妙想 API 服务类"""

    BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw"

    def __init__(self):
        """初始化，从配置获取 API Key"""
        config = get_config()
        self.api_key = config.mx_apikey

        if not self.api_key:
            logger.warning("未配置 MX_APIKEY，妙想功能不可用")

        self.headers = {
            "Content-Type": "application/json",
            "apikey": self.api_key,
        }

    def _request(self, endpoint: str, payload: Optional[Dict] = None) -> Optional[Dict[str, Any]]:
        """发送 POST 请求到妙想 API"""
        if not self.api_key:
            logger.error("MX_APIKEY 未配置，无法调用妙想 API")
            return None

        url = f"{self.BASE_URL}/{endpoint.lstrip('/')}"
        try:
            response = requests.post(url, headers=self.headers, json=payload or {}, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.warning("妙想 API 请求失败 [%s]: %s", endpoint, e)
            return None

    # ==================== 1. 金融数据查询 ====================

    def query_financial_data(self, tool_query: str) -> Optional[Dict[str, Any]]:
        """
        查询金融数据（行情、财务、关联关系等）

        Args:
            tool_query: 自然语言查询问句，如 "东方财富最新价"

        Returns:
            API 响应，data.data.searchDataResultDTO.dataTableDTOList 包含表格数据
        """
        return self._request("query", {"toolQuery": tool_query})

    # ==================== 2. 资讯搜索 ====================

    def search_news(self, query: str) -> Optional[Dict[str, Any]]:
        """
        搜索金融资讯（新闻、公告、研报等）

        Args:
            query: 搜索问句，如 "格力电器最新研报"

        Returns:
            API 响应，data.data.llmSearchResponse.data 包含资讯列表
        """
        return self._request("news-search", {"query": query})

    # ==================== 3. 智能选股 ====================

    def screen_stocks(
        self, keyword: str, page_no: int = 1, page_size: int = 20
    ) -> Tuple[List[Dict[str, str]], int]:
        """
        智能选股（低层动词：返回原始行）

        行是"中文列名 → 值"的 dict（列名可能带日期后缀），属 MX schema
        实现细节——策略/入口消费请用 screen_codes。

        Args:
            keyword:  自然语言选股条件，如 "今天A股价格大于10元"
            page_no:   页码，默认 1
            page_size: 每页条数，默认 20

        Returns:
            (行数据列表, 总条数)
        """
        result = self._request("stock-screen", {"keyword": keyword, "pageNo": page_no, "pageSize": page_size})
        if not result or result.get("status") != 0:
            logger.warning("智能选股失败: %s", result.get("message", "") if result else "无响应")
            return [], 0

        inner = result.get("data", {}).get("data", {})
        all_results = inner.get("allResults", {}).get("result", {})
        data_list = all_results.get("dataList", [])
        total = all_results.get("total", 0) or len(data_list)

        if not data_list:
            # 回退到 partialResults 解析
            partial = inner.get("partialResults", "")
            if partial:
                rows = self._parse_partial_results_table(partial)
                return rows, len(rows)
            return [], 0

        columns = all_results.get("columns", [])
        column_map = self._build_column_map(columns)
        column_order = self._columns_order(columns)
        rows = self._datalist_to_rows(data_list, column_map, column_order)
        return rows, total

    def screen_codes(self, keyword: str,
                     page_size: int = 30) -> List[Tuple[str, str]]:
        """智能选股的消费入口：条件关键词 → [(code, name), ...] 名单。

        fail-soft：请求失败/无行/解析异常一律返回空表，调用方按空名单兜底。
        """
        try:
            rows, total = self.screen_stocks(keyword, page_no=1,
                                             page_size=page_size)
            if not rows:
                logger.error(f"妙想选股无结果（关键词: {keyword}）")
                return []
            candidates = self._rows_to_codes(rows)
            logger.info(f"妙想选股返回 {total} 条，解析到 {len(candidates)} 只")
            return candidates
        except Exception as e:
            logger.error(f"妙想选股失败: {e}")
            return []

    @staticmethod
    def _rows_to_codes(rows: List[Dict[str, str]]) -> List[Tuple[str, str]]:
        """选股行 → (code, name)：中文列名防御式匹配（可带日期后缀），
        交易所前后缀经 data_provider.normalize_stock_code 归一。"""
        candidates = []
        for row in rows:
            code = None
            name = ""
            for k, v in row.items():
                ks = str(k)
                # 代码列：列名可能是"代码"或"股票代码"，排除"市场代码简称"
                if "市场" in ks:
                    continue
                if "代码" in ks and code is None:
                    code = str(v or "").strip()
                elif "简称" in ks or "名称" in ks:
                    name = str(v or "").strip()
            if code:
                code = normalize_stock_code(code)
                candidates.append((code, name or code))
        return candidates

    @staticmethod
    def _build_column_map(columns: List[Dict[str, Any]]) -> Dict[str, str]:
        """从 columns 构建英文列名 → 中文列名的映射"""
        name_map: Dict[str, str] = {}
        for col in columns or []:
            if not isinstance(col, dict):
                continue
            en_key = col.get("field") or col.get("name") or col.get("key", "")
            cn_name = col.get("displayName") or col.get("title") or col.get("label", "")
            date_msg = col.get("dateMsg", "")
            if date_msg:
                cn_name = f"{cn_name} {date_msg}"
            if en_key:
                name_map[str(en_key)] = str(cn_name)
        return name_map

    @staticmethod
    def _columns_order(columns: List[Dict[str, Any]]) -> List[str]:
        """按 columns 顺序返回原始列名列表"""
        order: List[str] = []
        for col in columns or []:
            if not isinstance(col, dict):
                continue
            en_key = col.get("field") or col.get("name") or col.get("key")
            if en_key is not None:
                order.append(str(en_key))
        return order

    @staticmethod
    def _datalist_to_rows(
        data_list: List[Dict[str, Any]],
        column_map: Dict[str, str],
        column_order: List[str],
    ) -> List[Dict[str, str]]:
        """将 data_list 中的键替换为中文列名"""
        import json
        if not data_list:
            return []
        first = data_list[0]
        extra_keys = [k for k in first if k not in column_order]
        header_order = column_order + extra_keys
        rows: List[Dict[str, str]] = []
        for row in data_list:
            if not isinstance(row, dict):
                continue
            cn_row: Dict[str, str] = {}
            for en_key in header_order:
                if en_key not in row:
                    continue
                cn_name = column_map.get(en_key, en_key)
                val = row[en_key]
                if val is None:
                    cn_row[cn_name] = ""
                elif isinstance(val, (dict, list)):
                    cn_row[cn_name] = json.dumps(val, ensure_ascii=False)
                else:
                    cn_row[cn_name] = str(val)
            rows.append(cn_row)
        return rows

    @staticmethod
    def _parse_partial_results_table(partial_results: str) -> List[Dict[str, str]]:
        """解析 partialResults 的 Markdown 表格为行字典列表"""
        import re
        if not partial_results or not isinstance(partial_results, str):
            return []
        lines = [ln.strip() for ln in partial_results.strip().splitlines() if ln.strip()]
        if not lines:
            return []

        def split_cells(line: str) -> List[str]:
            return [c.strip() for c in line.split("|") if c.strip() != ""]

        header_cells = split_cells(lines[0])
        if not header_cells:
            return []
        data_start = 1
        if data_start < len(lines) and re.match(r"^[\s\|\-]+$", lines[data_start]):
            data_start = 2
        rows: List[Dict[str, str]] = []
        for i in range(data_start, len(lines)):
            cells = split_cells(lines[i])
            if len(cells) < len(header_cells):
                cells.extend([""] * (len(header_cells) - len(cells)))
            elif len(cells) > len(header_cells):
                cells = cells[: len(header_cells)]
            rows.append(dict(zip(header_cells, cells)))
        return rows

    # ==================== 4. 自选股管理 ====================

    def fetch_self_selected(self) -> Tuple[List[str], Dict[str, str]]:
        """
        获取妙想自选股列表

        Returns:
            (股票代码列表, 代码到名称的映射)
        """
        result = self._request("self-select/get")
        if not result:
            return [], {}

        if result.get("status") != 0:
            logger.warning("获取妙想自选股失败: %s", result.get("message", "未知错误"))
            return [], {}

        data_list = (
            result.get("data", {})
            .get("allResults", {})
            .get("result", {})
            .get("dataList", [])
        )

        stock_codes = []
        name_mapping = {}
        for item in data_list:
            code = item.get("SECURITY_CODE", "").strip()
            name = item.get("SECURITY_SHORT_NAME", "").strip()
            if code:
                code = code.replace(".SH", "").replace(".SZ", "").replace(".BJ", "")
                stock_codes.append(code)
                if name:
                    name_mapping[code] = name

        logger.info("从妙想获取到 %d 只自选股", len(stock_codes))
        return stock_codes, name_mapping

    def add_self_select(self, stock_name_or_code: str) -> bool:
        """
        添加自选股

        Args:
            stock_name_or_code: 股票名称或代码，如 "贵州茅台" 或 "600519"

        Returns:
            是否成功
        """
        query_text = f"把{stock_name_or_code}添加到我的自选股列表"
        result = self._request("self-select/manage", {"query": query_text})
        if not result:
            return False
        if result.get("status") == 0:
            logger.info("已添加自选股: %s", stock_name_or_code)
            return True
        logger.warning("添加自选股失败 [%s]: %s", stock_name_or_code, result.get("message", ""))
        return False

    def remove_stocks(self, codes_to_remove: List[str]) -> bool:
        """
        从妙想自选股批量删除股票

        Args:
            codes_to_remove: 要删除的股票代码列表

        Returns:
            是否成功
        """
        query_text = f"把{codes_to_remove}从自选删除"
        result = self._request("self-select/manage", {"query": query_text})
        if not result:
            return False
        if result.get("status") == 0:
            logger.info("已从妙想批量删除 %d 只自选股", len(codes_to_remove))
            return True
        logger.warning("从妙想删除失败: %s", result.get("message", "未知错误"))
        return False

    # ==================== 5. 模拟组合管理 ====================

    def query_positions(self) -> Optional[Dict[str, Any]]:
        """
        查询模拟持仓

        Returns:
            data.posList 包含持仓明细
        """
        return self._request("mockTrading/positions")

    def query_balance(self) -> Optional[Dict[str, Any]]:
        """
        查询模拟资金账户

        Returns:
            data 包含 totalAssets/availBalance/frozenMoney 等
        """
        return self._request("mockTrading/balance")

    def query_orders(self, drt: int = 0, status: int = 0) -> Optional[Dict[str, Any]]:
        """
        查询委托记录

        Args:
            drt:    0=全部, 1=买入, 2=卖出
            status: 0=全部, 2=已报, 4=已成 等

        Returns:
            data.orders 包含委托列表
        """
        return self._request("mockTrading/orders", {"fltOrderDrt": drt, "fltOrderStatus": status})

    def place_order(
        self,
        order_type: str,
        stock_code: str,
        quantity: int,
        price: Optional[float] = None,
        use_market_price: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        模拟买卖委托

        Args:
            order_type:      "buy" 或 "sell"
            stock_code:      6 位股票代码
            quantity:        委托数量（100 的整数倍）
            price:           委托价格（市价时传 None）
            use_market_price: 是否以最新价委托

        Returns:
            data 包含 orderId/status
        """
        payload: Dict[str, Any] = {
            "type": order_type,
            "stockCode": stock_code,
            "quantity": quantity,
            "useMarketPrice": use_market_price,
        }
        if price is not None:
            payload["price"] = price
        return self._request("mockTrading/trade", payload)

    def buy_stock(self, stock_code: str, quantity: int, price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """快捷买入"""
        return self.place_order("buy", stock_code, quantity, price)

    def sell_stock(self, stock_code: str, quantity: int, price: Optional[float] = None) -> Optional[Dict[str, Any]]:
        """快捷卖出"""
        return self.place_order("sell", stock_code, quantity, price)

    def cancel_all_orders(self) -> Optional[Dict[str, Any]]:
        """一键撤单"""
        return self._request("mockTrading/cancel", {"type": "all"})

    def cancel_order(self, order_id: str, stock_code: str) -> Optional[Dict[str, Any]]:
        """按编号撤单"""
        return self._request("mockTrading/cancel", {"type": "order", "orderId": order_id, "stockCode": stock_code})
