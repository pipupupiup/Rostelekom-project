"""Ошибки подготовки данных.

Два класса ошибок разделены намеренно (задание ADO-01, п. 8) и соответствуют кодам API:

* `InputFormatError` — нарушение формата/схемы: нет обязательного поля, неверный тип, значение
  вне перечисления, некорректная дата → HTTP 400 `BAD_REQUEST`.
* `BusinessValidationError` — данные корректны по схеме, но противоречат справочникам или
  друг другу: неизвестный СЦ/узел/дом, дубликат ID, перерыв вне смены → HTTP 422.

Каждая ошибка содержит список `Issue(field, message)` с путём к полю (`orders[12].slot.end`),
что совпадает со схемой `ErrorResponse.details` спецификации.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Issue:
    field: str
    message: str

    def __str__(self) -> str:
        return f"{self.field}: {self.message}" if self.field else self.message


class DataPreparationError(Exception):
    code = "DATA_ERROR"

    def __init__(self, issues: list[Issue], summary: str | None = None) -> None:
        self.issues = list(issues)
        head = summary or (str(self.issues[0]) if self.issues else "ошибка данных")
        more = f" (и ещё {len(self.issues) - 1})" if len(self.issues) > 1 else ""
        super().__init__(head + more)


class InputFormatError(DataPreparationError):
    code = "BAD_REQUEST"


class BusinessValidationError(DataPreparationError):
    code = "UNPROCESSABLE"


@dataclass(slots=True)
class IssueCollector:
    """Накопитель ошибок: сначала собираем все проблемы, потом бросаем одним исключением."""

    format_issues: list[Issue] = field(default_factory=list)
    business_issues: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    def format(self, path: str, message: str) -> None:
        self.format_issues.append(Issue(path, message))

    def business(self, path: str, message: str) -> None:
        self.business_issues.append(Issue(path, message))

    def warn(self, path: str, message: str) -> None:
        self.warnings.append(Issue(path, message))

    def raise_format(self) -> None:
        if self.format_issues:
            raise InputFormatError(self.format_issues)

    def raise_all(self) -> None:
        """Ошибки формата важнее: при их наличии бизнес-проверки могли быть неполными."""
        self.raise_format()
        if self.business_issues:
            raise BusinessValidationError(self.business_issues)
