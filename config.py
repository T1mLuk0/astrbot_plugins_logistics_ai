from dataclasses import dataclass


@dataclass
class Config:

    api_url: str = "http://你的服务器IP:5000/chat"

    timeout: int = 60