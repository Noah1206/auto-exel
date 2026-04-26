"""주문 목록 QAbstractTableModel (PySide6).

엑셀과 동일한 컬럼 배열(입력 8 + 출력 2)을 그대로 보여주고,
유효하지 않은 행(RawRow)도 동일한 표 안에 포함한다.
사용자가 셀을 편집하면 재검증하여 Order 로 승격될 수 있다.
"""
from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from pydantic import ValidationError

from src.core.excel_manager import REQUIRED_COLUMNS, RawRow, RowItem
from src.models.order import Order
from src.utils.logger import get_logger

log = get_logger()

# 상태 컬럼은 delegate 가 직접 그리기 위해 상태 key 를 이 역할로 노출한다.
STATUS_KEY_ROLE = Qt.UserRole + 1

# 고정 컬럼: 상태 + 엑셀 입력 8개 + 엑셀 출력 2개 + 비고
# 엑셀 행번호(#) 는 vertical header 에 표시되므로 별도 컬럼 없음.
# key 가 REQUIRED_COLUMNS 안에 있으면 그 컬럼명으로 Order/RawRow 양쪽 조회.
META_COLS = [
    ("상태", "_status_display", False),
]
DATA_COLS = [(name, name, True) for name in REQUIRED_COLUMNS]
RESULT_COLS = [
    ("토탈가격", "_total_price_display", False),
    ("주문번호", "_order_number", False),
    ("비고", "_note", False),
]
COLUMNS = META_COLS + DATA_COLS + RESULT_COLS

# RawRow / Order 양쪽에서 입력 컬럼 이름으로 조회할 때 쓰는 Order 필드 매핑
_ORDER_FIELD_BY_COL = {
    "구매처": "product_url",
    "수취인": "name",
    "수취인번호": "phone",
    "통관번호": "customs_id",
    "우편번호": "postal_code",
    "수취인 주소": "address",
    "수량": "quantity",
    "영문이름": "english_name",
}

# 상태별 행 배경색 — 무채색만. 상태 구분은 상태 컬럼의 pill 배지에서 담당.
_STATUS_COLOR: dict[str, QColor | None] = {
    "completed": None,                # zebra 유지
    "in_progress": QColor("#F3F4F6"),
    "paused": None,
    "failed": None,
    "pending": None,
    "invalid": None,
    "unavailable": QColor("#E5E7EB"), # 회색 — "주문 불가" 톤다운
}

# 상태 컬럼 텍스트 색 — delegate 가 직접 그리므로 Qt 기본값을 검정으로
_STATUS_FG: dict[str, QColor] = {
    "completed": QColor("#111827"),
    "in_progress": QColor("#111827"),
    "paused": QColor("#111827"),
    "failed": QColor("#111827"),
    "pending": QColor("#6B7280"),
    "invalid": QColor("#111827"),
    "unavailable": QColor("#4B5563"),
}

_STATUS_EMOJI = {
    "pending": "\u25CB 대기",         # ○
    "in_progress": "\u25D0 진행중",   # ◐
    "paused": "\u23F8 수정 필요",     # ⏸ (선 글리프)
    "completed": "\u2713 완료",       # ✓
    "failed": "\u2715 실패",          # ✕
    "invalid": "\u25B3 값 확인",      # △
    "unavailable": "\u2300 판매 불가", # ⌀
}


class OrderTableModel(QAbstractTableModel):
    """Order + RawRow 혼합 리스트를 표시하는 테이블 모델."""

    def __init__(self, rows: Iterable[RowItem] | None = None, parent=None):
        super().__init__(parent)
        self._rows: list[RowItem] = list(rows) if rows else []
        # row_number → Order 승격 시 호출되는 Hook (ExcelManager.try_promote)
        self._promote_fn = None
        # 현재 진행 중인 엑셀 행 번호 (하이라이트용). None 이면 강조 없음.
        self._active_row: int | None = None
        # 현재 단계 메시지 (툴팁용)
        self._active_stage: str = ""

    def set_active_row(self, excel_row: int | None, stage: str = "") -> None:
        """현재 처리 중인 엑셀 행을 표시. None 이면 해제."""
        self._active_row = excel_row
        self._active_stage = stage
        # 전체 테이블 repaint (배경색/툴팁 변경 반영)
        if self._rows:
            top = self.index(0, 0)
            bot = self.index(
                len(self._rows) - 1, self.columnCount() - 1
            )
            self.dataChanged.emit(
                top, bot, [Qt.BackgroundRole, Qt.ToolTipRole]
            )

    # -------------------------------------------------------------
    # Data management
    # -------------------------------------------------------------

    def set_rows(self, rows: Iterable[RowItem]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    # 기존 API 호환 (Order 리스트만 받던 호출 지점 유지)
    def set_orders(self, orders: Iterable[RowItem]) -> None:
        self.set_rows(orders)

    def set_promote_fn(self, fn) -> None:
        """ExcelManager.try_promote(row, fields) 같은 콜백 등록."""
        self._promote_fn = fn

    def update_order(self, order: Order) -> None:
        for i, r in enumerate(self._rows):
            if getattr(r, "row", None) == order.row:
                self._rows[i] = order
                top = self.index(i, 0)
                bot = self.index(i, self.columnCount() - 1)
                self.dataChanged.emit(top, bot, [Qt.DisplayRole, Qt.BackgroundRole])
                return
        self.beginInsertRows(QModelIndex(), len(self._rows), len(self._rows))
        self._rows.append(order)
        self.endInsertRows()

    def get_row(self, idx: int) -> RowItem | None:
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    # 호환용 별칭 (과거 코드가 get_order() 호출하던 곳들)
    def get_order(self, idx: int) -> RowItem | None:
        return self.get_row(idx)

    def all_rows(self) -> list[RowItem]:
        return list(self._rows)

    def all_orders(self) -> list[RowItem]:  # 호환용
        return self.all_rows()

    def valid_orders(self) -> list[Order]:
        return [r for r in self._rows if isinstance(r, Order)]

    def invalid_rows(self) -> list[RawRow]:
        return [r for r in self._rows if isinstance(r, RawRow)]

    def missing_total_price(self) -> list[Order]:
        # unavailable 은 가격 못 얻은 게 정상이므로 제외
        return [
            r for r in self._rows
            if isinstance(r, Order) and r.needs_price() and r.status != "unavailable"
        ]

    def summary(self) -> dict[str, int]:
        counts = {
            "total": len(self._rows),
            "completed": 0,
            "in_progress": 0,
            "paused": 0,
            "pending": 0,
            "failed": 0,
            "invalid": 0,
            "unavailable": 0,
        }
        for r in self._rows:
            counts[getattr(r, "status", "pending")] = (
                counts.get(getattr(r, "status", "pending"), 0) + 1
            )
        return counts

    # -------------------------------------------------------------
    # Qt model API
    # -------------------------------------------------------------

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return COLUMNS[section][0]
            return None
        # Vertical: 엑셀 원본 행번호 (첫 행은 헤더이므로 데이터 행은 2부터)
        if role == Qt.DisplayRole:
            if 0 <= section < len(self._rows):
                return str(getattr(self._rows[section], "row", section + 2))
            return str(section + 2)
        if role == Qt.TextAlignmentRole:
            return int(Qt.AlignCenter)
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self._rows[index.row()]
        col_name, _, _ = COLUMNS[index.column()]

        # 상태 key 를 delegate 에서 조회할 수 있도록 노출
        if role == STATUS_KEY_ROLE:
            return getattr(item, "status", "pending")

        if role in (Qt.DisplayRole, Qt.EditRole):
            # 상태 컬럼은 StatusDelegate 가 직접 그리므로 빈 문자열 반환
            if col_name == "상태" and role == Qt.DisplayRole:
                return ""
            value = self._cell_value(item, col_name)
            if role == Qt.EditRole:
                return value
            return self._display_wrap(item, col_name, value)

        if role == Qt.BackgroundRole:
            # 진행 중인 행 배경은 기존 파란 떡칠 대신 아주 옅은 파랑으로만 표시 —
            # 상태 컬럼의 회전 스피너 pill 이 주된 진행중 표시 역할을 한다.
            status = getattr(item, "status", "pending")
            if (
                self._active_row is not None
                and getattr(item, "row", None) == self._active_row
            ):
                return QColor("#F3F4F6")
            color = _STATUS_COLOR.get(status)
            if color is not None:
                return color
            return None

        if role == Qt.ForegroundRole and col_name == "상태":
            status = getattr(item, "status", "pending")
            fg = _STATUS_FG.get(status)
            if fg is not None:
                return fg
            return None

        if role == Qt.ToolTipRole:
            # 현재 진행 중인 행이면 현재 단계를 툴팁으로
            if (
                self._active_row is not None
                and getattr(item, "row", None) == self._active_row
                and self._active_stage
            ):
                return f"▶ 진행 중: {self._active_stage}"
            # 긴 값은 셀 툴팁으로 전체 내용 보여주기 (구매처/수취인 주소/비고)
            if col_name in ("구매처", "수취인 주소"):
                full = self._cell_value(item, col_name)
                if full:
                    return full
            if isinstance(item, RawRow) and item.error:
                return f"검증 오류: {item.error}"
            if isinstance(item, Order) and item.error_message:
                return item.error_message

        if role == Qt.TextAlignmentRole:
            if col_name in ("수량", "토탈가격"):
                return int(Qt.AlignRight | Qt.AlignVCenter)
            if col_name == "상태":
                return int(Qt.AlignCenter)

        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if role != Qt.EditRole or not index.isValid():
            return False
        col_name, _, editable = COLUMNS[index.column()]
        if not editable:
            return False

        row_i = index.row()
        item = self._rows[row_i]
        # 완료된 주문은 입력 필드 변경 금지
        if isinstance(item, Order) and item.status == "completed":
            return False

        new_raw = "" if value is None else str(value).strip()

        # 현재 입력 8컬럼 값을 모두 끌어와서 그 중 하나만 새 값으로 갱신
        fields = self._fields_snapshot(item)
        fields[col_name] = new_raw

        promoted = self._build_updated_item(item, fields)

        self._rows[row_i] = promoted
        top = self.index(row_i, 0)
        bot = self.index(row_i, self.columnCount() - 1)
        self.dataChanged.emit(top, bot, [Qt.DisplayRole, Qt.BackgroundRole])
        return True

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        base = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        col_name, _, editable = COLUMNS[index.column()]
        item = self._rows[index.row()]
        completed = isinstance(item, Order) and item.status == "completed"
        if editable and not completed:
            base |= Qt.ItemIsEditable
        return base

    # -------------------------------------------------------------
    # Internals
    # -------------------------------------------------------------

    def _cell_value(self, item: RowItem, col_name: str) -> str:
        if col_name == "상태":
            return _STATUS_EMOJI.get(getattr(item, "status", "pending"), "")
        if col_name == "토탈가격":
            tp = getattr(item, "total_price", None)
            return f"{tp:,}" if tp is not None else ""
        if col_name == "주문번호":
            return getattr(item, "order_number", "") or ""
        if col_name == "비고":
            if isinstance(item, RawRow):
                return item.error
            return getattr(item, "error_message", "") or ""

        # 입력 8컬럼
        if isinstance(item, Order):
            field = _ORDER_FIELD_BY_COL.get(col_name)
            if field is None:
                return ""
            val = getattr(item, field, "")
            return "" if val is None else str(val)
        # RawRow
        return item.get(col_name)

    def _display_wrap(self, item: RowItem, col_name: str, value: str) -> str:
        if col_name == "구매처" and value:
            return self._shorten_url(value)
        return value

    @staticmethod
    def _shorten_url(url: str) -> str:
        """구매처 URL을 테이블에 보기 좋게 축약.
        예) https://www.11st.co.kr/products/12345 → 11st.co.kr/.../12345
        """
        import re

        m = re.match(r"^https?://([^/]+)(/.*)?$", url)
        if not m:
            return url if len(url) <= 40 else url[:37] + "..."
        host = m.group(1)
        path = m.group(2) or ""
        # 호스트 축약: www. 제거, 너무 길면 그대로
        host = host.removeprefix("www.")
        # 경로 마지막 세그먼트만
        tail = ""
        if path:
            parts = [p for p in path.split("/") if p]
            if parts:
                tail = "/.../" + parts[-1] if len(parts) > 1 else "/" + parts[0]
        compact = host + tail
        if len(compact) > 40:
            compact = compact[:37] + "..."
        return compact

    def _fields_snapshot(self, item: RowItem) -> dict[str, str]:
        out: dict[str, str] = {}
        for col_name in REQUIRED_COLUMNS:
            out[col_name] = self._cell_value(item, col_name)
        return out

    def _build_updated_item(self, old: RowItem, fields: dict[str, str]) -> RowItem:
        row_num = getattr(old, "row", 0)
        total_price = getattr(old, "total_price", None)
        order_number = getattr(old, "order_number", None)

        # 1) try_promote 콜백이 있으면 그걸로 시도
        if self._promote_fn is not None:
            try:
                new_item = self._promote_fn(row_num, fields)
                if isinstance(new_item, Order):
                    # 기존 Order 였던 경우의 메타(단가, 실행상태 등) 가능한 보존
                    if isinstance(old, Order):
                        new_item.unit_price = old.unit_price
                        new_item.total_price = (
                            old.unit_price * new_item.quantity
                            if old.unit_price is not None
                            else old.total_price
                        )
                        if new_item.status != "completed" and old.status in (
                            "failed",
                            "paused",
                        ):
                            new_item.status = "pending"
                            new_item.error_message = None
                    return new_item
                # RawRow 반환되면 그대로 반영
                return new_item
            except Exception as exc:
                log.warning(f"promote 콜백 오류: {exc}")

        # 2) 콜백 없으면 로컬에서 직접 Order 검증만 시도 (기본 경로)
        data: dict = {"row": row_num}
        for col_name, field_name in _ORDER_FIELD_BY_COL.items():
            data[field_name] = fields.get(col_name, "")
        if total_price is not None:
            data["total_price"] = total_price
            try:
                qty = int(float(fields.get("수량") or 1))
                if qty >= 1 and total_price % qty == 0:
                    data["unit_price"] = total_price // qty
            except (TypeError, ValueError):
                pass
        if order_number:
            data["order_number"] = order_number
            data["status"] = "completed"

        try:
            new_order = Order.model_validate(data)
            if isinstance(old, Order) and old.status in ("failed", "paused"):
                new_order.status = "pending"
                new_order.error_message = None
            return new_order
        except ValidationError as exc:
            return RawRow(
                row=row_num,
                fields=dict(fields),
                total_price=total_price,
                order_number=order_number,
                error=self._format_err(exc),
            )
        except Exception as exc:
            return RawRow(
                row=row_num,
                fields=dict(fields),
                total_price=total_price,
                order_number=order_number,
                error=str(exc),
            )

    @staticmethod
    def _format_err(exc: ValidationError) -> str:
        parts = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"])
            parts.append(f"{loc}: {err['msg']}")
        return "; ".join(parts)
