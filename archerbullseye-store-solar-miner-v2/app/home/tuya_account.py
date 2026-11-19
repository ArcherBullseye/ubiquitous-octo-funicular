"""Tuya QR sign-in and read-only device metadata lookup."""
import base64
import io
import json
import os
import re
import tempfile
import time
from pathlib import Path

CLIENT_ID = 'HA_3y9q4ak7g4ephrvke'
SCHEMA = 'haauthorize'


class AccountError(ValueError):
    pass


def account_path():
    from .. import db
    return db.DB_PATH.parent / 'tuya-account.json'


def read_account():
    try:
        return json.loads(account_path().read_text())
    except (FileNotFoundError, ValueError):
        return None


def write_account(account):
    path = account_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as file:
            temporary = Path(file.name)
            os.fchmod(file.fileno(), 0o600)
            json.dump(account, file)
        os.replace(temporary, path)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def forget_account():
    account_path().unlink(missing_ok=True)


def sdk():
    from tuya_sharing import LoginControl, Manager, logger
    # The SDK's debug logs can include decrypted account and device credentials.
    logger.disabled = True
    return LoginControl, Manager


def bounded_session(session):
    original = session.request
    def request(method, url, **kwargs):
        kwargs['timeout'] = 15
        return original(method, url, **kwargs)
    session.request = request


def begin_signin(user_code):
    if not isinstance(user_code, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', user_code):
        raise AccountError('Enter the User Code from Smart Life or Tuya Smart, under Account and Security.')
    LoginControl, _ = sdk()
    login = LoginControl()
    bounded_session(login.session)
    try:
        response = login.qr_code(CLIENT_ID, SCHEMA, user_code)
    finally:
        login.session.close()
    if not response or not response.get('success'):
        raise AccountError('Tuya could not create the QR code. Check the User Code and try again.')
    token = response['result']['qrcode']
    import qrcode
    from qrcode.image.svg import SvgPathImage
    image = qrcode.make('tuyaSmart--qrLogin?token=' + token, image_factory=SvgPathImage,
                        box_size=7, border=4)
    output = io.BytesIO()
    image.save(output)
    image_url = 'data:image/svg+xml;base64,' + base64.b64encode(output.getvalue()).decode()
    return {'user_code': user_code, 'qrcode': token}, image_url


def confirm_signin(pending):
    LoginControl, _ = sdk()
    login = LoginControl()
    bounded_session(login.session)
    try:
        success, info = login.login_result(pending['qrcode'], CLIENT_ID, pending['user_code'])
    finally:
        login.session.close()
    if not success:
        raise AccountError('The sign-in has not been approved. Scan the QR code, confirm in the app, then check again. If it expired, request a new code.')
    endpoint = info.get('endpoint', '')
    from urllib.parse import urlparse
    parsed = urlparse(endpoint)
    if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith('.iotbing.com'):
        raise AccountError('Tuya returned an unsupported account endpoint. Request a new QR code.')
    return {'user_code': pending['user_code'], 'terminal_id': info['terminal_id'],
            'endpoint': endpoint, 'token_info': {key: info[key] for key in
            ('t', 'uid', 'expire_time', 'access_token', 'refresh_token')}}


def fetch_devices(account, on_token):
    _, Manager = sdk()
    class TokenListener:
        def update_token(self, token_info):
            account['token_info'] = token_info
            on_token(account)
    manager = Manager(CLIENT_ID, account['user_code'], account['terminal_id'],
                      account['endpoint'], account['token_info'], TokenListener())
    bounded_session(manager.customer_api.session)
    devices = []
    warnings = []
    try:
        for home in manager.home_repository.query_homes():
            response = manager.customer_api.get('/v1.0/m/life/ha/home/devices', {'homeId': home.id})
            if not response or not response.get('success'):
                raise AccountError('Tuya could not list the devices in this account. Connect the account again.')
            for raw in response.get('result') or []:
                if not isinstance(raw, dict) or not raw.get('id'):
                    continue
                device = dict(raw, home_id=str(home.id))
                devices.append(device)
                if len(devices) > 200:
                    raise AccountError('This account contains more than 200 devices. Import from a smaller shared home.')
        deadline = time.monotonic() + 180
        for device in devices:
            if time.monotonic() > deadline:
                warnings.append('Some mappings could not be loaded in time. Load devices again to retry.')
                break
            # Raw DP relations retain node IDs and scales needed for local control.
            try:
                response = manager.customer_api.get(f'/v1.0/m/life/devices/{device["id"]}/status')
                device['relations'] = (response or {}).get('result', {}).get('dpStatusRelationDTOS', [])
            except Exception:
                warnings.append(f"Mapping unavailable for {device.get('name') or 'a device'}.")
    finally:
        manager.customer_api.session.close()
    return devices, warnings
