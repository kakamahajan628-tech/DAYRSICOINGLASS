import json
from pathlib import Path
import urllib.parse
import urllib.request
import uuid


class Telegram:
    def __init__(self, token, chat):
        self.token, self.chat = token, str(chat)

    def call(self, method, fields=None, file=None):
        fields = fields or {}
        if file:
            file = Path(file)
            boundary = "----Watchlist"+uuid.uuid4().hex
            pieces = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
                      for key, value in fields.items()]
            field="photo" if method=="sendPhoto" else "document"
            mime="image/png" if field=="photo" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            if file.stat().st_size>20*1024*1024: raise ValueError("REPORT_TOO_LARGE_USE_SHORTER_HISTORY")
            pieces += [f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{file.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode(),
                file.read_bytes(), f"\r\n--{boundary}--\r\n".encode()]
            body = b"".join(pieces)
            content_type = f"multipart/form-data; boundary={boundary}"
        else:
            body = urllib.parse.urlencode(fields).encode()
            content_type = "application/x-www-form-urlencoded"
        request = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=body,
            headers={"Content-Type": content_type})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except Exception as error:
            raise RuntimeError(f"Telegram {method} failed ({type(error).__name__}); delivery may be uncertain") from None
        if not result.get("ok"):
            raise RuntimeError(f"Telegram {method} rejected request")
        return result["result"]

    def text(self, text,buttons=False):
        fields={"chat_id":self.chat,"text":str(text)[:4000],"disable_notification":"true"}
        if buttons: fields["reply_markup"]=json.dumps({"inline_keyboard":[[{"text":"Daily Excel","callback_data":"daily"},{"text":"Weekly Excel","callback_data":"weeklycoin"}],[{"text":"Top gainers","callback_data":"top"},{"text":"Below 70","callback_data":"below70"}]]})
        return self.call("sendMessage",fields)

    def document(self, file, caption):
        return self.call("sendDocument", {"chat_id":self.chat,"caption":caption[:1024],"disable_notification":"true"}, file)

    def photo(self,file,caption):
        return self.call("sendPhoto",{"chat_id":self.chat,"caption":caption[:1024],"disable_notification":"true"},file)
