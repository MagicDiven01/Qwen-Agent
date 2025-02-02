from pydantic import BaseModel


class Record(BaseModel):
    url: str
    time: str
    type: str
    raw: list
    extract: str
    topic: str
    checked: bool
    session: list

    def to_dict(self) -> dict:
        return {
            'url': self.url,
            'time': self.time,
            'type': self.type,
            'raw': self.raw,
            'extract': self.extract,
            'topic': self.topic,
            'checked': self.checked,
            'session': self.session
        }


class PathConfig(BaseModel):
    work_space_root: str
    cache_root: str
    download_root: str
    code_interpreter_ws: str


class ServerConfig(BaseModel):
    server_host: str
    fast_api_port: int
    app_in_browser_port: int
    workstation_port: int
    model_server: str
    api_key: str
    llm: str
    max_ref_token: int
    max_days: int

    class Config:
        protected_namespaces = ()


class GlobalConfig(BaseModel):
    path: PathConfig
    server: ServerConfig
