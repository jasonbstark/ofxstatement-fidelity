import csv
import re
from decimal import Decimal, Decimal as D
from datetime import datetime
from typing import Dict, Optional, Any, TextIO
from os import path

from ofxstatement.plugin import Plugin
from ofxstatement.parser import AbstractStatementParser
from ofxstatement.statement import Statement, InvestStatementLine, StatementLine


class FidelityPlugin(Plugin):
    """Fidelity CSV plugin for ofxstatement"""

    def get_parser(self, filename: str) -> "FidelityCSVParser":
        return FidelityCSVParser(filename)


class FidelityCSVParser(AbstractStatementParser):
    statement: Statement
    fin: TextIO

    date_format: str = "%Y-%m-%d"
    cur_record: int = 0

    # Pre-compile regex patterns for performance
    mappings = [
        (re.compile(r"^REINVESTMENT "), "BUYSTOCK", "BUY"),
        (re.compile(r"^DIVIDEND RECEIVED "), "INCOME", "DIV"),
        (re.compile(r"^YOU BOUGHT "), "BUYSTOCK", "BUY"),
        (re.compile(r"^YOU SOLD "), "SELLSTOCK", "SELL"),
        (re.compile(r"^DIRECT DEBIT "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^Electronic Funds Transfer Paid "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^TRANSFERRED FROM "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^TRANSFERRED TO "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^DIRECT DEPOSIT "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^INTEREST EARNED "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^CONTRIBUTION "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^PARTIC CONTR "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^PARTIAL DISTRIBUTION "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^FED TAX W/H "), "INVBANKTRAN", "DEBIT"),
 # Begin added by Jason Stark, 2026 02 07
         (re.compile(r"^BILL PAYMENT "), "INVBANKTRAN", "DEBIT"),
         (re.compile(r"^Check Paid "), "INVBANKTRAN", "DEBIT"),
         (re.compile(r"^NORMAL DISTR PARTIAL "), "INVBANKTRAN", "DEBIT"),
         (re.compile(r"^STATE TAX W/H "), "INVBANKTRAN", "DEBIT"),
 # End added by Jason Stark, 2026 02 07
    ]

    def __init__(self, filename: str) -> None:
        super().__init__()
        self.filename = filename
        self.statement = Statement()
        self.statement.broker_id = "Fidelity"
        self.statement.currency = "USD"
        self.id_generator = IdGenerator()

    def parse_datetime(self, value: str) -> datetime:
        return datetime.strptime(value, self.date_format)

    def parse_decimal(self, value: str) -> D:
        # Remove thousand separators for US format (1,234.56 -> 1234.56)
        return D(value.replace(",", "").replace(" ", ""))

    def parse_value(self, value: Optional[str], field: str) -> Any:
        tp = StatementLine.__annotations__.get(field)
        if value is None:
            return None

        if tp in (datetime, Optional[datetime]):
            return self.parse_datetime(value)
        elif tp in (Decimal, Optional[Decimal]):
            return self.parse_decimal(value)
        else:
            return value

    def parse_record(self, line):
        # print(f"FidelityCSVParser:parse_record:entering line = {line}")
        """Parse given transaction line and return StatementLine object"""

        # Robustness: Check if the first column is a valid date.
        try:
            date = datetime.strptime(line[0][0:10], "%m/%d/%Y")
        except ValueError:
            # print(f"FidelityCSVParser:parse_record:returning ValueError: invalid date\n")
            return None

        line_length = len(line)
        if line_length == 14:
            # Fidelity multi-account Activity & Orders csv download

            # CSV Column Mapping Reference:
            # line[0 ] : Run Date
            # line[1 ] : Account
            # line[2 ] : Account Number
            # line[3 ] : Action
            # line[4 ] : Symbol
            # line[5 ] : Description
            # line[6 ] : Type
            # line[7 ] : Price ($)
            # line[8 ] : Quantity
            # line[9 ] : Commission ($)
            # line[10] : Fees ($)
            # line[11] : Accrued Interest ($)
            # line[12] : Amount ($)
            # line[13] : Settlement Date
            RUNDATE = 0
            ACCOUNT = 1
            ACCOUNTNUMBER = 2
            ACTION = 3
            SYMBOL = 4
            DESCRIPTION = 5
            TYPE = 6
            PRICE = 7
            QUANTITY = 8
            COMMISSION = 9
            FEES = 10
            ACRRUEDINTEREST = 11
            AMOUNT = 12
            SETTLEMENTDATE = 13
        elif line_length == 13:
            # Fidelity single account Activity & Orders csv download

            # line[0 ] : Run Date
            # line[1 ] : Action
            # line[2 ] : Symbol
            # line[3 ] : Description
            # line[4 ] : Type
            # line[5 ] : Price ($)
            # line[6 ] : Quantity
            # line[7 ] : Commission ($)
            # line[8 ] : Fees ($)
            # line[9 ] : Accrued Interest ($)
            # line[10] : Amount ($)
            # line[11] : Cash Balance ($)
            # line[12] : Settlement Date
            RUNDATE = 0
            ACTION = 1
            SYMBOL = 2
            DESCRIPTION = 3
            TYPE = 4
            PRICE = 5
            QUANTITY = 6
            COMMISSION = 7
            FEES = 8
            ACRRUEDINTEREST = 9
            AMOUNT = 10
            CASHBALANCE = 11
            SETTLEMENTDATE = 12
        else:
            # print(f"FidelityCSVParser:parse_record:returning line_length != 13\n")
            return None

        invest_stmt_line = InvestStatementLine()
        invest_stmt_line.date = date
        invest_stmt_line.date_user = date

        # Try to parse settlement date
        if line[SETTLEMENTDATE]:
            try:
                invest_stmt_line.date_user = datetime.strptime(
                    line[SETTLEMENTDATE][0:10], "%m/%d/%Y"
                )
            except ValueError:
                pass

        invest_stmt_line.memo = line[ACTION]

        # Common fields
        if line[FEES]:
            invest_stmt_line.fees = self.parse_decimal(line[FEES])
        if line[AMOUNT]:
            invest_stmt_line.amount = self.parse_decimal(line[AMOUNT])

        # 1. Identify the Transaction Type
        action = line[ACTION]
        for pattern, trntype, detailed in self.mappings:
            if pattern.match(action):
                invest_stmt_line.trntype = trntype
                invest_stmt_line.trntype_detailed = detailed
                break

        # 2. Extract Data based on Type
        if invest_stmt_line.trntype in ("BUYSTOCK", "SELLSTOCK"):
            invest_stmt_line.security_id = line[SYMBOL]
            # invest_stmt_line.unit_price = self.parse_decimal(line[PRICE])
            # invest_stmt_line.units = self.parse_decimal(line[QUANTITY])
            invest_stmt_line.units = self.parse_decimal(line[QUANTITY])
            invest_stmt_line.unit_price = self.parse_decimal(line[AMOUNT]) / self.parse_decimal(line[QUANTITY])

        elif (
            invest_stmt_line.trntype == "INCOME"
            and invest_stmt_line.trntype_detailed == "DIV"
        ):
            invest_stmt_line.security_id = line[SYMBOL]

        # print(f"FidelityCSVParser:parse_record:returning return = {invest_stmt_line}\n")
        return invest_stmt_line

    def parse(self) -> Statement:
        """Main entry point for parsers"""
        with open(self.filename, "r", encoding="utf-8-sig", newline="") as fin:
            self.fin = fin
            reader = csv.reader(self.fin)

            for csv_line in reader:
                self.cur_record += 1
                if not csv_line:
                    continue
                invest_stmt_line = self.parse_record(csv_line)
                if invest_stmt_line:
                    # Note: We do NOT validate here because IDs are assigned later
                    self.statement.invest_lines.append(invest_stmt_line)

            # derive account id from file name
            match = re.search(
                r".*History_for_Account_(.*)\.csv", path.basename(self.filename)
            )
            if match:
                self.statement.account_id = match[1]

            # reverse the lines to get Chronological Order (Oldest -> Newest)
            self.statement.invest_lines.reverse()

            # Generate IDs sequentially after sorting and VALIDATE
            for invest_line in self.statement.invest_lines:
                new_id = self.id_generator.create_id(invest_line.date)
                invest_line.id = new_id
                # Now that ID exists, we can validate the line
                invest_line.assert_valid()

            if self.statement.invest_lines:
                self.statement.start_date = min(
                    sl.date for sl in self.statement.invest_lines if sl.date is not None
                )
                self.statement.end_date = max(
                    sl.date for sl in self.statement.invest_lines if sl.date is not None
                )

            return self.statement


class IdGenerator:
    """Generates a unique ID based on the date"""

    def __init__(self) -> None:
        self.date_count: Dict[datetime, int] = {}

    def create_id(self, date) -> str:
        self.date_count[date] = self.date_count.get(date, 0) + 1
        return f'{datetime.strftime(date, "%Y%m%d")}-{self.date_count[date]}'
