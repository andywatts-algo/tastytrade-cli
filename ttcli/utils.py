import getpass
import logging
import os
import pickle
from pipes import quote
import shutil
from configparser import ConfigParser
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib.resources import as_file, files
from typing import Optional, List, Dict, Any

import asyncio
import aiohttp

from rich import print as rich_print
from tastytrade import Account, OrderAction, Session
from tastytrade.order import NewOrder, PlacedOrderResponse

logger = logging.getLogger(__name__)
VERSION = '0.2'
ZERO = Decimal(0)

CONTEXT_SETTINGS = {'help_option_names': ['-h', '--help']}

CUSTOM_CONFIG_PATH = '.config/ttcli/ttcli.cfg'
TOKEN_PATH = '.config/ttcli/.session'


def print_error(msg: str):
    rich_print(f'[bold red]Error: {msg}[/bold red]')


def print_warning(msg: str):
    rich_print(f'[light_coral]Warning: {msg}[/light_coral]')


def test_order_handle_errors(
    account: Account,
    session: 'RenewableSession',
    order: NewOrder
) -> Optional[PlacedOrderResponse]:
    url = f'{session.base_url}/accounts/{account.account_number}/orders/dry-run'
    json = order.model_dump_json(exclude_none=True, by_alias=True)
    response = session.client.post(url, data=json)
    # modified to use our error handling
    if response.status_code // 100 != 2:
        content = response.json()['error']
        print_error(f"{content['message']}")
        errors = content.get('errors')
        if errors is not None:
            for error in errors:
                if "code" in error:
                    print_error(f"{error['message']}")
                else:
                    print_error(f"{error['reason']}")
        return None
    else:
        data = response.json()['data']
        return PlacedOrderResponse(**data)


class RenewableSession(Session):
    def __init__(self):
        custom_path = os.path.join(os.path.expanduser('~'), CUSTOM_CONFIG_PATH)
        data_file = files('ttcli.data').joinpath('ttcli.cfg')
        token_path = os.path.join(os.path.expanduser('~'), TOKEN_PATH)

        logged_in = False
        # try to load token
        if os.path.exists(token_path):
            with open(token_path, 'rb') as f:
                self.__dict__ = pickle.load(f)

            # make sure token hasn't expired
            logged_in = self.validate()

        # load config
        self.config = ConfigParser()
        if not os.path.exists(custom_path):
            with as_file(data_file) as path:
                # copy default config to user home dir
                os.makedirs(os.path.dirname(custom_path), exist_ok=True)
                shutil.copyfile(path, custom_path)
                self.config.read(path)
        self.config.read(custom_path)

        if not logged_in:
            # either the token expired or doesn't exist
            username, password = self._get_credentials()
            Session.__init__(self, username, password)

            accounts = Account.get_accounts(self)
            self.accounts = [acc for acc in accounts if not acc.is_closed]
            # write session token to cache
            os.makedirs(os.path.dirname(token_path), exist_ok=True)
            with open(token_path, 'wb') as f:
                pickle.dump(self.__dict__, f)
            logger.debug('Logged in with new session, cached for next login.')
        else:
            logger.debug('Logged in with cached session.')

    def _get_credentials(self):
        username = os.getenv('TT_USERNAME')
        password = os.getenv('TT_PASSWORD')
        if self.config.has_section('general'):
            username = username or self.config['general'].get('username')
            password = password or self.config['general'].get('password')

        if not username:
            username = getpass.getpass('Username: ')
        if not password:
            password = getpass.getpass('Password: ')

        return username, password

    def get_account(self) -> Account:
        account = self.config['general'].get('default-account', None)
        if account:
            try:
                return next(a for a in self.accounts if a.account_number == account)
            except StopIteration:
                print_warning('Default account is set, but the account doesn\'t appear to exist!')

        for i in range(len(self.accounts)):
            if i == 0:
                print(f'{i + 1}) {self.accounts[i].account_number} '
                      f'{self.accounts[i].nickname} (default)')
            else:
                print(f'{i + 1}) {self.accounts[i].account_number} {self.accounts[i].nickname}')
        choice = 0
        while choice not in range(1, len(self.accounts) + 1):
            try:
                raw = input('Please choose an account: ')
                choice = int(raw)
            except ValueError:
                return self.accounts[0]
        return self.accounts[choice - 1]


def is_monthly(day: date) -> bool:
    return day.weekday() == 4 and 15 <= day.day <= 21


def get_confirmation(prompt: str, default: bool = True) -> bool:
    while True:
        answer = input(prompt).lower()
        if not answer:
            return default
        if answer[0] == 'y':
            return True
        if answer[0] == 'n':
            return False


async def post_to_optionstrat(
    symbol: str,
    order: NewOrder,
    name: str,
    description: str,
    price: Decimal
) -> Dict[str, Any]:
    """
    JSON POST trade to optionstrat.com/api/strategy.

    :param symbol: The underlying symbol for the trade
    :param order: The NewOrder object containing the trade details
    :param name: Name of the trade
    :param description: Description of the trade
    :param price: The price of the option or spread
    :return: The response from the API as a dictionary
    """
    legs = []
    for leg in order.legs:
        legs.append({
            "revision": 0,
            "enabled": True,
            "symbol": leg.instrument_type.symbol,
            "basis": float(price),
            "quantity": leg.quantity if leg.action in [OrderAction.BUY_TO_OPEN, OrderAction.BUY_TO_CLOSE] else -leg.quantity
        })

    url = "https://optionstrat.com/api/strategy"
    
    payload = {
        "name": name,
        "isCustomName": True,
        "description": description,
        "strategy": {
            "isCashSecured": False,
            "symbol": symbol,
            "items": legs
        }
    }

    # Cookie SID from environment variable
    sid = os.getenv('OPTIONSTRAT_SID')
    if not sid:
        raise ValueError("OPTIONSTRAT_SID environment variable is not set")
    expiration_time = datetime.utcnow() + timedelta(hours=24)
    expiration_str = expiration_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
    cookie = f"sid={quote(sid)}; Path=/; Expires={expiration_str};"
    headers = { "Cookie": cookie }

    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as response:
            if response.status == 200:
                return await response.json()
            else:
                raise Exception(f"Failed to post to OptionStrat. Status: {response.status}, Response: {await response.text()}")



