import csv
import re
import sys
from datetime import datetime, date, time, timedelta
from typing import Dict, Optional, Any, TextIO
from os import path
from pathlib import Path
from json import load as jsonload
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

    def __init__(self, filename: str) -> None:
        super().__init__()
        self.filename = filename
        self.statement = Statement()
        self.statement.line_dict = {}
        self.statement.broker_id = "Fidelity"
        self.statement.currency = "USD"
        self.df_statement = None
        self.match_lookback_days = 0
        self.match_lookforward_days = 0

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

        else:
            return None

        invest_stmt_line = InvestStatementLine()

        try:
            date = datetime.strptime(line[RUNDATE][0:10], "%m/%d/%Y")
        except ValueError:
            return None
        invest_stmt_line.Date = date

        if line[ACCOUNTNUMBER]:
            invest_stmt_line.Account = line[ACCOUNTNUMBER]

        invest_stmt_line.Description = line[ACTION]

        if line[SYMBOL]:
            invest_stmt_line.Symbol = line[SYMBOL]
        else:
            invest_stmt_line.Symbol = ""

        if line[QUANTITY]:
            invest_stmt_line.Amount = self.parse_decimal(line[QUANTITY])

        if line[PRICE]:
            invest_stmt_line.Price = self.parse_decimal(line[PRICE])

        if line[AMOUNT]:
            sign_amount = np.sign(Decimal(line[AMOUNT]))
            invest_stmt_line.Value = self.parse_decimal(line[AMOUNT])

        if line[ACCOUNT]:
            invest_stmt_line.account_type = line[ACCOUNT]

        if line[FEES]:
            invest_stmt_line.fees = self.parse_decimal(line[FEES])

        if line[SETTLEMENTDATE]:
            try:
                invest_stmt_line.date_user = datetime.strptime(
                    line[SETTLEMENTDATE][0:10], "%m/%d/%Y"
                )
            except ValueError:
                pass

        invest_stmt_line = self.provide_pricing(invest_stmt_line)

        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + f'{invest_stmt_line.Description}'
        id_trx = self.id_str_generate(id_string)
        invest_stmt_line.TransactionID = id_trx

        invest_stmt_lines = [invest_stmt_line]

        translation = self.matching_translation(invest_stmt_line)
        account = invest_stmt_line.Account
        symbol = invest_stmt_line.Symbol

        if translation is not None:
            if translation["Type"] == "Transfer":
                invest_stmt_line.Account = translation["Account"].replace("[Account]", account)
                invest_stmt_line.Type = "Transfer"
                invest_stmt_line.Status = "t"

            elif translation["Type"] == "Stock":
                invest_stmt_line.Account = translation["Account"].replace("[Account]", account)
                invest_stmt_line.Account_Fees = translation["Account_Fees"]
                invest_stmt_line.Symbol = translation["Symbol"].replace("[Symbol]", symbol)
                invest_stmt_line.Type = "Stock"
                invest_stmt_line.Status = "s"
                invest_stmt_lines = self.buildStockTransactions(invest_stmt_line)

            elif translation["Type"] == "Mortgage":
                account_mortgage = translation["Account_Mortgage"]
                account_escrow = translation["Account_Escrow"]
                account_interest = translation["Account_Interest"]
                interest_rate = Decimal(translation["Interest_Rate"])
                mortgage_principal_interest = Decimal(translation["Principal + Interest"])
                invest_stmt_line.Type = "Mortgage"
                invest_stmt_line.Status = "m"
                invest_stmt_lines = self.buildMortgageTransactions(invest_stmt_line, account_mortgage, account_escrow, account_interest, interest_rate, mortgage_principal_interest)

            elif (translation["Type"] == "Assignment") and (translation["Account"] != "Uncategorized"):
                invest_stmt_line.Type = "Assignment"
                invest_stmt_line.Status = "c"
                invest_stmt_lines = self.buildAssignmentTransactions(invest_stmt_line, translation["Account"])

            elif (translation["Type"] == "Assignment") and (translation["Account"] == "Uncategorized"):
                invest_stmt_line.Type = "Assignment"
                invest_stmt_line.Status = "n"
                target_account = translation["Account"]
                invest_stmt_lines = self.buildAssignmentTransactions(invest_stmt_line, target_account)
        else:
            invest_stmt_line.Type = "None"
            invest_stmt_line.Status = "n"

        self.statement.line_dict[self.freeze(invest_stmt_line)] = translation
        return invest_stmt_lines

    def buildAssignmentTransactions(self, invest_stmt_line, account_target):
        invest_stmt_lines = []

        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.Description
        id_trx = self.id_str_generate(id_string)
        invest_stmt_line.TransactionID = id_trx

        invest_stmt_line_account = InvestStatementLine()
        invest_stmt_line_account.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_account.Symbol = ""
        invest_stmt_line_account.Price = Decimal(1)
        invest_stmt_line_account.Amount = invest_stmt_line_account.Value
        invest_stmt_lines.append(invest_stmt_line_account)

        invest_stmt_line_assignment = InvestStatementLine()
        invest_stmt_line_assignment.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_assignment.Account = account_target.replace("[Account]",  invest_stmt_line.Account).replace("[Symbol]",  invest_stmt_line.Symbol)
        invest_stmt_line_assignment.Price = Decimal(1)
        invest_stmt_line_assignment.Amount = -invest_stmt_line.Value
        invest_stmt_line_assignment.Value = -invest_stmt_line.Value
        invest_stmt_lines.append(invest_stmt_line_assignment)

        return invest_stmt_lines

    def buildStockTransactions(self, invest_stmt_line):
        invest_stmt_lines = []

        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.Description
        id_trx = self.id_str_generate(id_string)
        invest_stmt_line.TransactionID = id_trx

        invest_stmt_line_account = InvestStatementLine()
        invest_stmt_line_account.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_account.Symbol = ""
        invest_stmt_line_account.Price = Decimal(1)
        invest_stmt_line_account.Amount = invest_stmt_line_account.Value
        invest_stmt_lines.append(invest_stmt_line_account)

        invest_stmt_line_stock = InvestStatementLine()
        invest_stmt_line_stock.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_stock.Account = invest_stmt_line_stock.Account + ":" + invest_stmt_line_stock.Symbol
        invest_stmt_line_stock.Value = (Decimal(invest_stmt_line_stock.Amount) * Decimal(invest_stmt_line_stock.Price)).quantize(TWOPLACES)

        has_fees = hasattr(invest_stmt_line, "fees") and (invest_stmt_line.fees is not None)
        if has_fees:
            invest_stmt_line_stock.Price = Decimal((Decimal(invest_stmt_line_stock.Value) - Decimal(invest_stmt_line.fees)) / Decimal(invest_stmt_line_stock.Amount)).quantize(TWOPLACES)
        else:
            invest_stmt_line_stock.Price = (Decimal(invest_stmt_line_stock.Value) / Decimal(invest_stmt_line_stock.Amount)).quantize(TWOPLACES)
        fees = -(Decimal(invest_stmt_line_stock.Price) * Decimal(invest_stmt_line_stock.Amount) + Decimal(invest_stmt_line_account.Value)).quantize(TWOPLACES)

        invest_stmt_lines.append(invest_stmt_line_stock)

        if fees != Decimal(0):
            invest_stmt_line_fees = InvestStatementLine()
            invest_stmt_line_fees.__dict__ = invest_stmt_line.__dict__.copy()
            invest_stmt_line_fees.Account = invest_stmt_line_fees.Account_Fees.replace("[Account]",  invest_stmt_line.Account)
            invest_stmt_line_fees.Price = Decimal(1)
            invest_stmt_line_fees.Amount = fees
            invest_stmt_line_fees.Value = fees
            invest_stmt_lines.append(invest_stmt_line_fees)

        return invest_stmt_lines

    def buildMortgageTransactions(self, invest_stmt_line, account_mortgage, account_escrow, account_interest, interest_rate, mortgage_principal_interest):
        try:
            self.book
        except AttributeError:
            self.book = self.initialize_book()
            self.mortgage_balance = (-self.account_balance(self.book,  account_mortgage, invest_stmt_line.Date + timedelta(days=-1))).quantize(TWOPLACES)

        invest_lines = []

        id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.Description
        id_trx = self.id_str_generate(id_string)
        invest_stmt_line.TransactionID = id_trx

        mortgage_payment = -Decimal(invest_stmt_line.Value).quantize(TWOPLACES)
        mortgage_interest = (self.mortgage_balance * interest_rate / Decimal(1200)).quantize(TWOPLACES)
        mortgage_principal = (mortgage_principal_interest - mortgage_interest).quantize(TWOPLACES)
        mortgage_escrow = (mortgage_payment - mortgage_principal_interest).quantize(TWOPLACES)
        mortgage_delta = mortgage_payment - mortgage_principal - mortgage_interest - mortgage_escrow

        if mortgage_delta != Decimal(0):
            sys.exit(ValueError)

        invest_stmt_line_principal = InvestStatementLine()
        invest_stmt_line_principal.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_principal.Account = account_mortgage
        invest_stmt_line_principal.Value = mortgage_principal
        invest_stmt_line_principal.Price = Decimal(1)
        invest_stmt_line_principal.Amount = mortgage_principal
        invest_stmt_line_principal.TransactionID = id_trx        
        self.mortgage_balance -= mortgage_principal

        invest_stmt_line_interest = InvestStatementLine()
        invest_stmt_line_interest.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_interest.Account = account_interest
        invest_stmt_line_interest.Value = mortgage_interest
        invest_stmt_line_interest.Price = Decimal(1)
        invest_stmt_line_interest.Amount = mortgage_interest
        invest_stmt_line_interest.TransactionID = id_trx        

        invest_stmt_line_escrow = InvestStatementLine()
        invest_stmt_line_escrow.__dict__ = invest_stmt_line.__dict__.copy()
        invest_stmt_line_escrow.Account = account_escrow
        invest_stmt_line_escrow.Value = mortgage_escrow
        invest_stmt_line_escrow.Price = Decimal(1)
        invest_stmt_line_escrow.Amount = mortgage_escrow
        invest_stmt_line_escrow.TransactionID = id_trx        

        invest_lines.append(invest_stmt_line)
        invest_lines.append(invest_stmt_line_principal)
        invest_lines.append(invest_stmt_line_interest)
        invest_lines.append(invest_stmt_line_escrow)

        return invest_lines
        
    def freeze(self, d):
        if isinstance(d, dict):
            return frozenset((key, freeze(value)) for key, value in d.items())
        elif isinstance(d, list):
            return tuple(freeze(value) for value in d)
        return d

    def match_translation(self, translation, invest_stmt_line):
        DateStr = f'{datetime.strftime(invest_stmt_line.Date, "%Y-%m-%d")}'
        Date_match = translation["Mapping"]["Date"].search(DateStr)
        Account_match = translation["Mapping"]["Account"].search(invest_stmt_line.Account)
        Description_match = translation["Mapping"]["Description"].search(invest_stmt_line.Description)
        Symbol_match = translation["Mapping"]["Symbol"].search(invest_stmt_line.Symbol)
        Amount_match = translation["Mapping"]["Amount"].search('{0:.2f}'.format(invest_stmt_line.Value))
        Price_match = translation["Mapping"]["Price"].search('{0:.2f}'.format(invest_stmt_line.Price))
        Value_match = translation["Mapping"]["Value"].search('{0:.2f}'.format(invest_stmt_line.Value))
        pattern_matched = Date_match and Account_match and Description_match and Symbol_match and Amount_match and Price_match and Value_match
        pattern_match = {"Translation": pattern_matched, "Date": Date_match, "Account": Account_match, "Description": Description_match, "Symbol": Symbol_match, "Amount": Amount_match, "Price": Price_match, "Value": Value_match}
        return pattern_match

    def matching_translation(self, invest_stmt_line):
        for translation in self.translations:
            pattern_matched = self.match_translation(translation, invest_stmt_line)
            if pattern_matched["Translation"]:
                return translation

    def parse(self) -> Statement:
        """Main entry point for parsers"""

        self.get_translations()
        
        csv_in = self.get_csv()

        for csv_line in csv_in:
            invest_stmt_lines = self.parse_record(csv_line)
            if invest_stmt_lines:
                self.statement.invest_lines.extend(invest_stmt_lines)

        self.statement_to_df()

        self.process_transfers()

        self.df_statement.sort_values(by=['Date', 'TransactionID'], ascending=[True, True], inplace=True)

        self.df_to_statement()

        if self.statement.invest_lines:
            self.statement.start_date = min(
                sl.Date for sl in self.statement.invest_lines if sl.Date is not None
            )
            self.statement.end_date = max(
                sl.Date for sl in self.statement.invest_lines if sl.Date is not None
            )

        return self.statement

    def get_csv(self):
        with open(self.filename, "r", encoding="utf-8-sig", newline="") as fin:
            self.fin = fin
            reader = csv.reader(self.fin)

            csv_in = []
            date_initial = None
            for csv_line in reader:
                self.cur_record += 1
                if not csv_line:
                    continue
                try:
                    date = datetime.strptime(csv_line[0], "%m/%d/%Y")
                except ValueError:
                    continue
                if date_initial is None:
                    date_initial = date
                csv_in.append(csv_line)

            date_final = date

            if date_initial > date_final:
                csv_in.reverse()
        return csv_in

    def process_transfers(self):
        if "Transfer" in self.df_types.keys():
            df_types_transfer = self.df_types["Transfer"][::-1]
            for index, row in df_types_transfer.iterrows():
                if row['Type'] == "Transfer":
                    mask_date = (self.df_types["Transfer"]['Date'] >= row['Date'] - timedelta(days=self.match_lookback_days)) \
                        & (self.df_types["Transfer"]['Date'] <= row['Date'] + timedelta(days=self.match_lookforward_days))
                    mask_amount = (self.df_types["Transfer"]['Value'] == -row['Value'])

                    df_match_date = self.df_types["Transfer"][mask_date]
                    df_match = self.df_types["Transfer"][mask_date & mask_amount]

                    df_match_length = df_match.shape[0]
                    if df_match_length == 1:
                        index_match = df_match.index[0]

                        self.df_types["Transfer"].loc[index, 'TransactionID'] = self.df_types["Transfer"].loc[index_match, 'TransactionID']
                        self.df_statement.loc[index, 'TransactionID'] = self.df_types["Transfer"].loc[index_match, 'TransactionID']
                        self.df_types["Transfer"].loc[index, 'Description'] = self.df_types["Transfer"].loc[index_match, 'Description']
                        self.df_statement.loc[index, 'Description'] = self.df_types["Transfer"].loc[index_match, 'Description']
        return

    def statement_to_df(self):
        ld = []
        for line in self.statement.invest_lines:
            d = line.__dict__
            ld.append(d)

        df_statement = pd.DataFrame(ld)

        statement_cols = df_statement.columns
        cols = ["Date","Account","Description","TransactionCommodity","Amount","Price","Value", "Status","TransactionID", "Type"]
        for col in cols:
            if col not in statement_cols:
                df_statement[col] = pd.Series()

        statement_cols = df_statement.columns
        # newcols = [col for col in cols if col in statement_cols] + [col for col in statement_cols if col not in cols]
        newcols = [col for col in cols if col in statement_cols]
        df_statement = df_statement[newcols]
        self.df_statement = df_statement

        self.types_transaction = sorted(self.df_statement["Type"].unique(), reverse=True)
        self.df_types = {}
        for type_transaction in self.types_transaction:
            self.df_types[type_transaction] = self.df_statement[self.df_statement["Type"] == type_transaction]

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
            invest_stmt_line.Status = row['Status']
            invest_stmt_line.TransactionID = row['TransactionID']

            self.statement.invest_lines.append(invest_stmt_line)

    def get_translations(self):
        """Returns list of dictionaries with translations.json data"""
        pathTranslations = Path("/Volumes/Nextcloud Data/Nextcloud/Jason's documents/computer/repo/ofxstatement-fidelity/src/ofxstatement_fidelity/translations.json")
        self.pathTranslations = pathTranslations
        with pathTranslations.open() as translationjson:
            self.translations = jsonload(translationjson)

        self.transfers = []
        self.mortgages = []
        self.stocks = []
        self.assignments = []
        for translation in self.translations:
            translation["Mapping"]["Date"] = re.compile(translation["Mapping"]["Date"])
            translation["Mapping"]["Account"] = re.compile(translation["Mapping"]["Account"])
            translation["Mapping"]["Description"] = re.compile(translation["Mapping"]["Description"])
            translation["Mapping"]["Symbol"] = re.compile(translation["Mapping"]["Symbol"])
            translation["Mapping"]["Amount"] = re.compile(translation["Mapping"]["Amount"])
            translation["Mapping"]["Price"] = re.compile(translation["Mapping"]["Price"])
            translation["Mapping"]["Value"] = re.compile(translation["Mapping"]["Value"])

            if translation["Type"] == "Transfer":
                self.transfers.append(translation)
            elif translation["Type"] == "Stock":
                self.stocks.append(translation)
            elif translation["Type"] == "Mortgage":
                self.mortgages.append(translation)
            elif translation["Type"] == "Assignment":
                self.assignments.append(translation)

    # def buildStockTransactions(self, invest_stmt_line):
    #     invest_lines = []
    #
    #     id_string = f'{datetime.strftime(datetime.now(), "%Y-%m-%d %H:%M:%S.%f")}, ' + invest_stmt_line.Account + ", " + invest_stmt_line.trntype + ", " + invest_stmt_line.trntype_detailed
    #     id_trx = self.id_str_generate(id_string)
    #
    #     invest_stmt_line_stock = InvestStatementLine()
    #     invest_stmt_line_stock.__dict__ = invest_stmt_line.__dict__.copy()
    #
    #     account = self.stocks_dict[invest_stmt_line_stock.Account][invest_stmt_line_stock.TransactionCommodity]
    #     invest_stmt_line_stock.Account = account
    #     invest_stmt_line_stock.Value = -invest_stmt_line.Value
    #     invest_stmt_line_stock.TransactionID = id_trx
    #
    #     invest_stmt_line.Price = Decimal(1)
    #     invest_stmt_line.Value = invest_stmt_line.Value
    #     invest_stmt_line.TransactionID = id_trx
    #
    #     invest_lines.append(invest_stmt_line)
    #     invest_lines.append(invest_stmt_line_stock)
    #
    #     return invest_lines
    #
    def provide_pricing(self, invest_line):
        if invest_line.Price is None:
            invest_line.Price = Decimal(1)
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
