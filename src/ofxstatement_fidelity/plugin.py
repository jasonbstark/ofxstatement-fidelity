import csv
import re
from datetime import datetime, date, time, timedelta
from typing import Dict, Optional, Any, TextIO
from os import path
import hashlib
import pandas as pd
import numpy as np
from decimal import Decimal
TWOPLACES = Decimal(10) ** -2
SIXPLACES = Decimal(10) ** -6

from ofxstatement.plugin import Plugin
from ofxstatement.parser import AbstractStatementParser
from ofxstatement.statement import Statement, InvestStatementLine, StatementLine

from gncxml_integration import Book, copy_gnucash_accounts

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
    mappings_memo = [
        (re.compile(r"^REINVESTMENT "), "BUYSTOCK", "BUY"),
        (re.compile(r"^DIVIDEND RECEIVED "), "INCOME", "DIV"),
        (re.compile(r"^YOU BOUGHT "), "BUYSTOCK", "BUY"),
        (re.compile(r"^YOU SOLD "), "SELLSTOCK", "SELL"),
        (re.compile(r"^DIRECT DEBIT "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^Electronic Funds Transfer Paid "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^TRANSFERRED FROM "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^TRANSFERRED TO "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^DIRECT DEPOSIT "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^INTEREST EARNED "), "INCOME", "DIV"),
        (re.compile(r"^CONTRIBUTION "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^PARTIC CONTR "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^PARTIAL DISTRIBUTION "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^FED TAX W/H "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^CASH ADVANCE "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^ADJUST FEE CHARGED ATM FEE REBATE "), "INVBANKTRAN", "CREDIT"),
        (re.compile(r"^BILL PAYMENT "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^Check Paid "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^NORMAL DISTR PARTIAL "), "INVBANKTRAN", "DEBIT"),
        (re.compile(r"^STATE TAX W/H "), "INVBANKTRAN", "DEBIT"),
    ]

    mappings_accounts = [
        (re.compile(r"^X59128643"), "Fidelity:Fidelity X59-128643"),
        (re.compile(r"^159258482"), "Fidelity:Fidelity 159258482 (Jason)"),
        (re.compile(r"^352042315"), "Fidelity:Fidelity 352042315 (Elisa)"),
    ]

    stocks_dict =   {"Fidelity:Fidelity 159258482 (Jason)":  
                        {
                            "NVDA": "Fidelity:Fidelity 159258482 (Jason):NVDA", 
                            "CRWV": "Fidelity:Fidelity 159258482 (Jason):CRWV"
                        },
                    "Fidelity:Fidelity 352042315 (Elisa)":  
                        {
                            "MCD": "Fidelity:Fidelity 352-042315 (Elisa):MCDONALDS CORP"
                        },
                    
                    }

    mortgage_pattern = re.compile(r"^DIRECT DEBIT FREEDOM MTG PYMTS")


    def __init__(self, filename: str) -> None:
        super().__init__()
        self.filename = filename
        self.statement = Statement()
        self.statement.broker_id = "Fidelity"
        self.statement.currency = "USD"
        self.df_statement = None
        self.match_lookback_days = 0

        self.mortgage_account = "Real Estate:Mortgage Amerisave"
        self.interest_account = "Interest:Mortgage"
        self.escrow_account = "Real Estate:Escrow Amerisave"
        self.mortgage_rate = Decimal(2.75)
        self.mortgage_principal_interest = Decimal(2570.94)

    def parse_datetime(self, value: str) -> datetime:
        return datetime.strptime(value, self.date_format)

    def parse_decimal(self, value: str) -> Decimal:
        return Decimal(value.replace(",", "").replace(" ", ""))

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
        """Parse given transaction line and return StatementLine object"""
        # print(f"line = {line}")

        line_length = len(line)
        if line_length == 14:
            self.multi_account = True
            # Fidelity multi-account Activity & Orders csv download

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
            self.multi_account = False
            # Fidelity single account Activity & Orders csv download

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
            return None

        try:
            date = datetime.strptime(line[RUNDATE][0:10], "%m/%d/%Y")
        except ValueError:
            return None

        invest_stmt_line = InvestStatementLine()
        invest_stmt_line.date = date

        if line[SETTLEMENTDATE]:
            try:
                invest_stmt_line.date_user = datetime.strptime(
                    line[SETTLEMENTDATE][0:10], "%m/%d/%Y"
                )
            except ValueError:
                pass

        invest_stmt_line.memo = line[ACTION].replace("315994103", "FDRXX")

        if line[ACCOUNT]:
            invest_stmt_line.account_type = line[ACCOUNT]

        if line[ACCOUNTNUMBER]:
            for pattern, name in self.mappings_accounts:
                if pattern.match(line[ACCOUNTNUMBER]):
                    invest_stmt_line.account = name
                    break

        if line[FEES]:
            invest_stmt_line.fees = self.parse_decimal(line[FEES])

        if line[AMOUNT]:
            sign_amount = np.sign(Decimal(line[AMOUNT]))
            invest_stmt_line.amount = self.parse_decimal(line[AMOUNT])

        action = line[ACTION]
        for pattern, trntype, detailed in self.mappings_memo:
            if pattern.match(action):
                invest_stmt_line.trntype = trntype
                invest_stmt_line.trntype_detailed = detailed
                break

        if invest_stmt_line.trntype in ("BUYSTOCK", "SELLSTOCK"):
            invest_stmt_line.security_id = line[SYMBOL].replace("315994103", "FDRXX")
            invest_stmt_line.units = sign_amount * self.parse_decimal(line[QUANTITY])
            invest_stmt_line.unit_price = Decimal(abs(self.parse_decimal(line[AMOUNT]) / self.parse_decimal(line[QUANTITY]))).quantize(Decimal(10) ** -6)

        elif (invest_stmt_line.trntype == "INCOME" and invest_stmt_line.trntype_detailed == "DIV"):
            invest_stmt_line.security_id = line[SYMBOL].replace("315994103", "FDRXX")
            invest_stmt_line.units = self.parse_decimal(line[AMOUNT])
            invest_stmt_line.unit_price = Decimal(1).quantize(Decimal(10) ** -6)

        if ("REINVESTMENT CASH (FDRXX)" in invest_stmt_line.memo) or ("REINVESTMENT FIDELITY GOVERNMENT CASH RESERVES (FDRXX)" in invest_stmt_line.memo):
            invest_stmt_line.account = "Income:Dividends:" + invest_stmt_line.account.split(sep=":")[1] + ":FIDELITY CASH RESERVES"

        invest_stmt_line = self.provide_pricing(invest_stmt_line)

        if self.mortgage_pattern.match(invest_stmt_line.memo):
            invest_stmt_lines = self.buildMortgageTransactions(invest_stmt_line)
        elif (invest_stmt_line.trntype in ("BUYSTOCK", "SELLSTOCK")) and ("REINVESTMENT CASH (FDRXX)" not in invest_stmt_line.memo):
            invest_stmt_lines = self.buildStockTransactions(invest_stmt_line)
        else:
            id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
            id_trx = self.id_str_generate(id_string)
            invest_stmt_line.id_trx = id_trx
            invest_stmt_lines = [invest_stmt_line]

        return invest_stmt_lines

    def parse(self) -> Statement:
        """Main entry point for parsers"""
        with open(self.filename, "r", encoding="utf-8-sig", newline="") as fin:
            self.fin = fin
            reader = csv.reader(self.fin)

            for csv_line in reader:
                self.cur_record += 1
                if not csv_line:
                    continue
                invest_stmt_lines = self.parse_record(csv_line)

                if invest_stmt_lines:
                    self.statement.invest_lines.extend(invest_stmt_lines)

            if self.multi_account:
                    self.statement.account_id = "multi-account csv file"
            else:
                match = re.search(
                    r".*History_for_Account_(.*)\.csv", path.basename(self.filename)
                )
                if match:
                    self.statement.account_id = match[1]

            invest_lines = self.statement.invest_lines.copy()
            invest_lines_reversed = invest_lines.copy()
            invest_lines_reversed.reverse()
            for invest_line in invest_lines_reversed:
                id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_line.account + ", " + invest_line.trntype + ", " + invest_line.trntype_detailed
                invest_line.id_split = self.id_str_generate(id_string)
                invest_line.assert_valid()

            ld = []
            for line in self.statement.invest_lines:
                d = line.__dict__
                ld.append(d)
            ld.reverse()

            df_statement = pd.DataFrame(ld)

            df_cols = df_statement.columns
            cols = ["date","account","memo","security_id","units","unit_price","amount","id_trx", "id_split"]
            for col in cols:
                if col not in df_cols:
                    df_statement[col] = pd.Series()
            statement_cols = df_statement.columns
            newcols = [col for col in cols if col in statement_cols] + [col for col in statement_cols if col not in cols]
            df_statement = df_statement[newcols]

            self.df_statement = df_statement
            print(f"self.df_statement = \n{self.df_statement}")

            self.statement.invest_lines = []
            for invest_line in invest_lines_reversed:
                self.statement.invest_lines.append(invest_line)
                invest_line.assert_valid()

            self.statement.invest_lines.reverse()

            if self.statement.invest_lines:
                self.statement.start_date = min(
                    sl.date for sl in self.statement.invest_lines if sl.date is not None
                )
                self.statement.end_date = max(
                    sl.date for sl in self.statement.invest_lines if sl.date is not None
                )

            return self.statement

    def sort_if_necessary(self, df, column):
        if not df[column].is_monotonic_increasing:
            return df.sort_values(by=column)
        return df

    def buildStockTransactions(self, invest_stmt_line):
        invest_lines = []
        
        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
        id_trx = self.id_str_generate(id_string)

        invest_stmt_line_stock = InvestStatementLine()
        invest_stmt_line_stock.__dict__ = invest_stmt_line.__dict__.copy()

        account = self.stocks_dict[invest_stmt_line_stock.account][invest_stmt_line_stock.security_id]
        invest_stmt_line_stock.account = account
        invest_stmt_line_stock.amount = -invest_stmt_line_stock.amount
        invest_stmt_line_stock.id_trx = id_trx

        invest_stmt_line.unit_price = Decimal(1).quantize(SIXPLACES)
        invest_stmt_line.units = invest_stmt_line.amount
        invest_stmt_line.id_trx = id_trx        

        invest_lines.append(invest_stmt_line)
        invest_lines.append(invest_stmt_line_stock)

        return invest_lines
        
    def buildMortgageTransactions(self, invest_stmt_line):
        try:
            self.book
        except AttributeError:
            self.book = self.initialize_book()
            self.mortgage_balance = (-self.account_balance(self.book,  self.mortgage_account, invest_stmt_line.date + timedelta(days=-1))).quantize(TWOPLACES)

        invest_lines = []

        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
        id_trx = self.id_str_generate(id_string)

        invest_stmt_line.id_trx = id_trx        

        mortgage_payment = -Decimal(invest_stmt_line.amount).quantize(TWOPLACES)
        mortgage_interest = Decimal(self.mortgage_balance * self.mortgage_rate / 1200).quantize(TWOPLACES)
        mortgage_principal = (self.mortgage_principal_interest - mortgage_interest).quantize(TWOPLACES)
        mortgage_escrow = (mortgage_payment - self.mortgage_principal_interest).quantize(TWOPLACES)
        mortgage_delta = mortgage_payment - mortgage_principal - mortgage_interest - mortgage_escrow
        if mortgage_delta != Decimal(0):
            print(f"buildMortgage:  Error, mortgage payment not sum of principal, interest and escrow")

        invest_stmt_line_principal = InvestStatementLine()
        invest_stmt_line_principal.__dict__ = invest_stmt_line.__dict__.copy()
        # invest_stmt_line_principal.date = None
        # invest_stmt_line_principal.memo = None
        invest_stmt_line_principal.account = self.mortgage_account
        invest_stmt_line_principal.amount = mortgage_principal
        invest_stmt_line_principal.units = mortgage_principal
        invest_stmt_line_principal.id_trx = id_trx        
        self.mortgage_balance -= mortgage_principal

        invest_stmt_line_interest = InvestStatementLine()
        invest_stmt_line_interest.__dict__ = invest_stmt_line.__dict__.copy()
        # invest_stmt_line_interest.date = None
        # invest_stmt_line_interest.memo = None
        invest_stmt_line_interest.account = self.interest_account
        invest_stmt_line_interest.amount = mortgage_interest
        invest_stmt_line_interest.units = mortgage_interest
        invest_stmt_line_interest.id_trx = id_trx        

        invest_stmt_line_escrow = InvestStatementLine()
        invest_stmt_line_escrow.__dict__ = invest_stmt_line.__dict__.copy()
        # invest_stmt_line_escrow.date = None
        # invest_stmt_line_escrow.memo = None
        invest_stmt_line_escrow.account = self.escrow_account
        invest_stmt_line_escrow.amount = mortgage_escrow
        invest_stmt_line_escrow.units = mortgage_escrow
        invest_stmt_line_escrow.id_trx = id_trx        

        invest_lines.append(invest_stmt_line)
        invest_lines.append(invest_stmt_line_principal)
        invest_lines.append(invest_stmt_line_interest)
        invest_lines.append(invest_stmt_line_escrow)

        return invest_lines
        
    def provide_pricing(self, invest_line):
        if invest_line.unit_price is None:
            invest_line.unit_price = Decimal(1).quantize(SIXPLACES)
            invest_line.units = invest_line.amount

        return invest_line

    def initialize_book(self) -> Book:
        bookname = copy_gnucash_accounts()

        try:
            book = Book(bookname)
        except OSError as err:
            sys.exit(err)

        return book

    def account_balance(self, book, account, date = None):
        id = book.accounts[book.accounts['path'] == account].index.values[0][1]
        splits = book.splits[book.splits['act_id'] == id].sort_values(by='trn_date')
        splits['balance'] = splits['value'].cumsum()
    
        if date is None:
            balance = splits['balance'].iloc[-1]
        else:
            balance = splits[splits['trn_date'] <= date]['balance'].iloc[-1]

        return balance

    def id_str_generate(self, seed=""):
        m = hashlib.sha256(seed.encode('utf-8'))
        str_hash = m.hexdigest()[0:32]
        return str_hash

class IdGenerator:
    """Generates a unique ID based on the date"""

    def __init__(self) -> None:
        self.date_count: Dict[datetime, int] = {}

    def create_id(self, date) -> str:
        self.date_count[date] = self.date_count.get(date, 0) + 1
        return f'{datetime.strftime(date, "%Y%m%d")}-{self.date_count[date]}'

