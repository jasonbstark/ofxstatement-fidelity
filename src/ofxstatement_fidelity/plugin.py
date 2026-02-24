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

    dividends_dict =   {"Fidelity:Fidelity 159258482 (Jason)":  
                        {
                            "NVDA": "Income:Dividends:Fidelity 159258482 (Jason):NVDA", 
                            "CRWV": "Income:Dividends:Fidelity 159258482 (Jason):CRWV"
                        },
                    "Fidelity:Fidelity 352042315 (Elisa)":  
                        {
                            "MCD": "Income:Dividends:Fidelity 352-042315 (Elisa):MCD"
                        },
                    
                    }

    stocks_dict =   {"Fidelity:Fidelity 159258482 (Jason)":  
                        {
                            "NVDA": "Fidelity:Fidelity 159258482 (Jason):NVDA", 
                            "CRWV": "Fidelity:Fidelity 159258482 (Jason):CRWV"
                        },
                    "Fidelity:Fidelity 352042315 (Elisa)":  
                        {
                            "MCD": "Fidelity:Fidelity 352-042315 (Elisa):MCD"
                        },
                    
                    }

    mortgage_pattern = re.compile(r"^DIRECT DEBIT FREEDOM MTG PYMTS")
    
    citicard_account = "Citi Costco Visa"


    def __init__(self, filename: str) -> None:
        super().__init__()
        self.filename = filename
        self.statement = Statement()
        self.statement.broker_id = "Fidelity"
        self.statement.currency = "USD"
        self.df_statement = None
        self.match_lookback_days = 0
        self.match_lookforward_days = 0

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
        invest_stmt_line.Date = date

        if line[SETTLEMENTDATE]:
            try:
                invest_stmt_line.date_user = datetime.strptime(
                    line[SETTLEMENTDATE][0:10], "%m/%d/%Y"
                )
            except ValueError:
                pass

        invest_stmt_line.Description = line[ACTION].replace("315994103", "FDRXX")

        if line[ACCOUNT]:
            invest_stmt_line.account_type = line[ACCOUNT]

        if line[ACCOUNTNUMBER]:
            for pattern, name in self.mappings_accounts:
                if pattern.match(line[ACCOUNTNUMBER]):
                    invest_stmt_line.Account = name
                    break

        if line[FEES]:
            invest_stmt_line.fees = self.parse_decimal(line[FEES])

        if line[AMOUNT]:
            sign_amount = np.sign(Decimal(line[AMOUNT]))
            invest_stmt_line.Value = self.parse_decimal(line[AMOUNT])

        action = line[ACTION]
        for pattern, trntype, detailed in self.mappings_memo:
            if pattern.match(action):
                invest_stmt_line.trntype = trntype
                invest_stmt_line.trntype_detailed = detailed
                break

        if invest_stmt_line.trntype in ("BUYSTOCK", "SELLSTOCK"):
            invest_stmt_line.TransactionCommodity = line[SYMBOL].replace("315994103", "FDRXX")
            invest_stmt_line.Amount = sign_amount * self.parse_decimal(line[QUANTITY])
            invest_stmt_line.Price = Decimal(abs(self.parse_decimal(line[AMOUNT]) / self.parse_decimal(line[QUANTITY]))).quantize(Decimal(10) ** -6)

        elif (invest_stmt_line.trntype == "INCOME" and invest_stmt_line.trntype_detailed == "DIV"):
            invest_stmt_line.TransactionCommodity = line[SYMBOL].replace("315994103", "FDRXX")
            invest_stmt_line.Amount = self.parse_decimal(line[AMOUNT])
            invest_stmt_line.Price = Decimal(1).quantize(Decimal(10) ** -6)

        if ("REINVESTMENT CASH (FDRXX)" in invest_stmt_line.Description) \
            or ("REINVESTMENT FIDELITY GOVERNMENT CASH RESERVES (FDRXX)" in invest_stmt_line.Description):
            invest_stmt_line.Account = "Income:Dividends:" + invest_stmt_line.Account.split(sep=":")[1] + ":FDRXX"

        invest_stmt_line = self.provide_pricing(invest_stmt_line)

        if self.mortgage_pattern.match(invest_stmt_line.Description):
            invest_stmt_lines = self.buildMortgageTransactions(invest_stmt_line)
        elif (invest_stmt_line.trntype in ("BUYSTOCK", "SELLSTOCK")) \
            and ("REINVESTMENT CASH (FDRXX)" not in invest_stmt_line.Description) \
            and ("REINVESTMENT FIDELITY GOVERNMENT CASH RESERVES (FDRXX)" not in invest_stmt_line.Description):
            invest_stmt_lines = self.buildStockTransactions(invest_stmt_line)
        elif ("DIVIDEND RECEIVED " in invest_stmt_line.Description) and ("FDRXX" not in invest_stmt_line.Description):
            invest_stmt_lines = self.buildDividendTransactions(invest_stmt_line)
        elif ("DIRECT DEBIT CITI CARD ONLIPAYMENT" in invest_stmt_line.Description):
            invest_stmt_lines = self.buildCitiCardTransfer(invest_stmt_line)
        else:
            id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
            id_trx = self.id_str_generate(id_string)
            invest_stmt_line.TransactionID = id_trx
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

            self.statement_to_df()

            self.process_transfers()

            self.df_statement.sort_values(by=['Date', 'TransactionID'], ascending=[True, True], inplace=True)

            self.df_to_statement()

            self.statement.invest_lines.reverse()

            if self.statement.invest_lines:
                self.statement.start_date = min(
                    sl.Date for sl in self.statement.invest_lines if sl.Date is not None
                )
                self.statement.end_date = max(
                    sl.Date for sl in self.statement.invest_lines if sl.Date is not None
                )

            return self.statement

    def process_transfers(self):
        for index, row in self.df_statement.iterrows():
            if ("TRANSFERRED FROM VS " in row['Description']) or ("REINVESTMENT CASH (FDRXX)" in row['Description']) or ("REINVESTMENT FIDELITY GOVERNMENT CASH RESERVES (FDRXX)" in row['Description']):
                mask_date = (self.df_statement['Date'] >= row['Date'] - timedelta(days=self.match_lookback_days)) & (self.df_statement['Date'] <= row['Date'] + timedelta(days=self.match_lookforward_days))
                mask_amount = (self.df_statement['Value'] == -row['Value'])

                df_match_date = self.df_statement[mask_date]
                df_match = self.df_statement[mask_date & mask_amount]

                df_match_length = df_match.shape[0]
                if df_match_length == 1:
                    index_match = df_match.index[0]
                    self.df_statement.loc[index, 'TransactionID'] = self.df_statement.loc[index_match, 'TransactionID']
                    self.df_statement.loc[index, 'Description'] = self.df_statement.loc[index_match, 'Description']
        return

    def buildCitiCardTransfer(self, invest_stmt_line):
        invest_lines = []
        
        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
        id_trx = self.id_str_generate(id_string)

        invest_stmt_line_citicard = InvestStatementLine()
        invest_stmt_line_citicard.__dict__ = invest_stmt_line.__dict__.copy()

        invest_stmt_line_citicard.account_type = "Credit Card"
        invest_stmt_line_citicard.Account = self.citicard_account
        invest_stmt_line_citicard.Amount = -invest_stmt_line.Value
        invest_stmt_line_citicard.Price = Decimal(1).quantize(SIXPLACES)
        invest_stmt_line_citicard.Value = -invest_stmt_line.Value
        invest_stmt_line_citicard.TransactionID = id_trx

        invest_stmt_line.Price = Decimal(1).quantize(SIXPLACES)
        invest_stmt_line.Amount = invest_stmt_line.Value
        invest_stmt_line.TransactionID = id_trx        

        invest_lines.append(invest_stmt_line)
        invest_lines.append(invest_stmt_line_citicard)

        return invest_lines
        
    def buildDividendTransactions(self, invest_stmt_line):
        invest_lines = []
        
        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
        id_trx = self.id_str_generate(id_string)

        invest_stmt_line_dividend = InvestStatementLine()
        invest_stmt_line_dividend.__dict__ = invest_stmt_line.__dict__.copy()

        account = self.dividends_dict[invest_stmt_line_dividend.account][invest_stmt_line_dividend.security_id]
        invest_stmt_line_dividend.Account = account
        invest_stmt_line_dividend.Value = -invest_stmt_line.Value
        invest_stmt_line_dividend.TransactionID = id_trx

        invest_stmt_line.Price = Decimal(1).quantize(SIXPLACES)
        invest_stmt_line.Amount = invest_stmt_line.Value
        invest_stmt_line.TransactionID = id_trx        

        invest_lines.append(invest_stmt_line)
        invest_lines.append(invest_stmt_line_dividend)

        return invest_lines
        
    def buildStockTransactions(self, invest_stmt_line):
        invest_lines = []
        
        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
        id_trx = self.id_str_generate(id_string)

        invest_stmt_line_stock = InvestStatementLine()
        invest_stmt_line_stock.__dict__ = invest_stmt_line.__dict__.copy()

        account = self.stocks_dict[invest_stmt_line_stock.Account][invest_stmt_line_stock.TransactionCommodity]
        invest_stmt_line_stock.Account = account
        invest_stmt_line_stock.Value = -invest_stmt_line.Value
        invest_stmt_line_stock.TransactionID = id_trx

        invest_stmt_line.Price = Decimal(1).quantize(SIXPLACES)
        invest_stmt_line.Value = invest_stmt_line.Value
        invest_stmt_line.TransactionID = id_trx        

        invest_lines.append(invest_stmt_line)
        invest_lines.append(invest_stmt_line_stock)

        return invest_lines
        
    def buildMortgageTransactions(self, invest_stmt_line):
        try:
            self.book
        except AttributeError:
            self.book = self.initialize_book()
            self.mortgage_balance = (-self.account_balance(self.book,  self.mortgage_account, invest_stmt_line.Date + timedelta(days=-1))).quantize(TWOPLACES)

        invest_lines = []

        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
        id_trx = self.id_str_generate(id_string)

        invest_stmt_line.TransactionID = id_trx

        mortgage_payment = -Decimal(invest_stmt_line.Value).quantize(TWOPLACES)
        mortgage_interest = Decimal(self.mortgage_balance * self.mortgage_rate / 1200).quantize(TWOPLACES)
        mortgage_principal = (self.mortgage_principal_interest - mortgage_interest).quantize(TWOPLACES)
        mortgage_escrow = (mortgage_payment - self.mortgage_principal_interest).quantize(TWOPLACES)
        mortgage_delta = mortgage_payment - mortgage_principal - mortgage_interest - mortgage_escrow
        if mortgage_delta != Decimal(0):
            sys.exit(ValueError)

        invest_stmt_line_principal = InvestStatementLine()
        invest_stmt_line_principal.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_principal.Account = self.mortgage_account
        invest_stmt_line_principal.Value = mortgage_principal
        invest_stmt_line_principal.Price = Decimal(1).quantize(SIXPLACES)
        invest_stmt_line_principal.Amount = mortgage_principal
        invest_stmt_line_principal.TransactionID = id_trx        
        self.mortgage_balance -= mortgage_principal

        invest_stmt_line_interest = InvestStatementLine()
        invest_stmt_line_interest.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_interest.Account = self.interest_account
        invest_stmt_line_interest.Value = mortgage_interest
        invest_stmt_line_interest.Price = Decimal(1).quantize(SIXPLACES)
        invest_stmt_line_interest.Amount = mortgage_interest
        invest_stmt_line_interest.TransactionID = id_trx        

        invest_stmt_line_escrow = InvestStatementLine()
        invest_stmt_line_escrow.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_escrow.Account = self.escrow_account
        invest_stmt_line_escrow.Value = mortgage_escrow
        invest_stmt_line_escrow.Price = Decimal(1).quantize(SIXPLACES)
        invest_stmt_line_escrow.Amount = mortgage_escrow
        invest_stmt_line_escrow.TransactionID = id_trx        

        invest_lines.append(invest_stmt_line)
        invest_lines.append(invest_stmt_line_principal)
        invest_lines.append(invest_stmt_line_interest)
        invest_lines.append(invest_stmt_line_escrow)

        return invest_lines
        
    def statement_to_df(self):
        ld = []
        for line in self.statement.invest_lines:
            d = line.__dict__
            ld.append(d)
        ld.reverse()

        df_statement = pd.DataFrame(ld)

        statement_cols = df_statement.columns
        cols = ["Date","Account","Description","TransactionCommodity","Amount","Price","Value","TransactionID", "id_split"]
        for col in cols:
            if col not in statement_cols:
                df_statement[col] = pd.Series()

        statement_cols = df_statement.columns
        newcols = [col for col in cols if col in statement_cols] + [col for col in statement_cols if col not in cols]
        df_statement = df_statement[newcols]

        self.df_statement = df_statement

    def df_to_statement(self):
        self.statement.invest_lines = []
        for index, row in self.df_statement.iterrows():
            invest_stmt_line = InvestStatementLine()
            invest_stmt_line.Date = row['Date']
            invest_stmt_line.Account = row['Account']
            invest_stmt_line.Description = row['Description']
            invest_stmt_line.TransactionCommodity = row['TransactionCommodity']
            invest_stmt_line.Amount = row['Amount']
            invest_stmt_line.Price = row['Price']
            invest_stmt_line.Value = row['Value']
            invest_stmt_line.TransactionID = row['TransactionID']
            invest_stmt_line.id_split = row['id_split']
            invest_stmt_line.trntype = row['trntype']
            invest_stmt_line.trntype_detailed = row['trntype_detailed']
            invest_stmt_line.account_type = row['account_type']

            id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
            invest_stmt_line.id_split = self.id_str_generate(id_string)
            invest_stmt_line.assert_valid()

            self.statement.invest_lines.append(invest_stmt_line)
        
    def provide_pricing(self, invest_line):
        if invest_line.Price is None:
            invest_line.Price = Decimal(1).quantize(SIXPLACES)
            invest_line.Amount = invest_line.Value

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

