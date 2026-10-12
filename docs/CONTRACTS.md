# F1 uygulama sözleşmeleri

Bu belge paralel geliştirmede aynı arayüzün kullanılmasını sağlar. Kodun nihai
tipleri kaynak otoritesidir; sözleşme değişirse bütün tüketiciler güncellenir.

## Ayarlar (root sorumluluğu)

`jarvis.config.settings`: `ModelConfig(provider: Literal[openrouter,anthropic,ollama],
model: str, input_per_million: Decimal, output_per_million: Decimal,
context_window: int=32768)`. `Settings`: `workspace: Path`, `state_dir: Path`,
`audit_path: Path`, `models: dict[str,ModelConfig]`, `primary_model: str`,
`fallback_model: str|None`, `max_steps=8`, `max_seconds=60.0`,
`max_output_tokens=1024`, `daily_budget_usd: Decimal=1`, `allow_paid=False`,
`allow_cloud_file_content=False`, `allow_cloud_clipboard=False`,
`fallback_seconds=60.0`, `applications: dict[str,str]`,
`openrouter_base_url`, `anthropic_base_url`, `ollama_base_url`.
`load_settings(path: Path) -> Settings`. Anahtarlar ayar nesnesinde bulunmaz.

## Araç ve kapı (safety geliştiricisi)

`jarvis.tools.base`: `Risk` str Enum READ_ONLY, SIDE_EFFECT, CONFIRM_REQUIRED.
`ToolResult` pydantic: `success: bool=False`, `status: Literal[ok,error,denied,sent]`,
`message: str`, `data: dict[str,Any]`, `external: bool=False`,
`content_kind: Literal[text,file,clipboard,web]=text`.
`ToolDefinition` pydantic: `name`, `description`, `parameters: dict[str,Any]`, `risk`.
`BaseTool` sınıf alanları: `name`, `description`, `args_model: type[BaseModel]`,
`risk=SIDE_EFFECT`, `external=False`, `content_kind=text`.
`async execute(self, arguments: BaseModel, context: ToolContext) -> ToolResult`.
`ToolContext` dataclass: `workspace: Path`, `settings: Settings`, `audit: AuditLog`,
`cancelled: asyncio.Event` (default factory), `services: dict[str,Any]` (default).

`jarvis.tools.registry`: `ToolRegistry.register(cls)`, `.get(name) -> BaseTool|None`,
`.definitions() -> list[ToolDefinition]`; `register_tool` global dekoratör;
`discover_tools() -> ToolRegistry` (plugins paketini otomatik keşfeder).
İsim sağlayıcı formatıyla zaten uyumludur; kodlama eşlemesi identity + doğrulama.

`jarvis.safety.gate`: `TurnState(turn_id=str UUID default, tainted=False)`;
`SafetyGate(registry)`, `.pending_notice: str`,
`async dispatch(call: ToolCall, context: ToolContext, state: TurnState,
approve: Callable[[str,dict[str,Any]],bool]) -> ToolResult`.
Reddedilen çağrı sonucu status=denied; tur tekrar denetimi AgentLoop'tadır.
`jarvis.safety.paths.safe_join(base: Path,target: str) -> Path`.
`jarvis.safety.audit.AuditLog(path: Path)`, `.emit(event: str, **fields: Any) -> None`.
Sır temizleme AuditLog içinde; secret değerleri ortamdan okunur ve repr edilmez.

## Beyin modelleri ve sağlayıcılar (provider geliştiricisi)

`jarvis.brain.models`: pydantic `ToolCall(id:str,name:str,arguments:dict[str,Any])`;
`Message(role: Literal[system,user,assistant,tool], content:str='',
tool_calls:list[ToolCall]=[], tool_call_id:str|None=None, private:bool=False)`;
`Usage(input_tokens:int=0,output_tokens:int=0,cost_usd:Decimal=0)`;
`Completion(content:str='',tool_calls:list[ToolCall]=[],usage:Usage=Usage(),
provider:str='',model:str='',local_fallback:bool=False)`.

`jarvis.brain.providers`: `Provider` Protocol:
`async complete(messages:list[Message],tools:list[ToolDefinition],*,timeout:float)
-> Completion`.
`ProviderError` (mesajında ham response/token bulunmaz),
`BudgetExceeded(ProviderError)`.
`OpenRouterProvider(settings, model:ModelConfig)`, `AnthropicProvider(...)`,
`OllamaProvider(...)`; ortamdan gerekli sırlar çağrı sırasında okunur.
`build_provider(settings:Settings,audit:AuditLog) -> Provider` router döndürür.
Router model başına kullanım/maliyet loglar, SQLite günlük bütçe rezervasyonu
uygular; maliyet ön kontrolünde güvenli context_window + max_output_tokens sınırı
kullanır. Bulut mesajları private ise içerik maskelenir. Ollama tam içeriği alır.
Yedek dönüşünde Completion.local_fallback=True, AgentLoop bir araç sınırı uygular.
`jarvis.brain.fake.ScriptedProvider(responses:list[Completion])` root yazacak.

## Döngü ve koşucu (root)

`AgentLoop(provider:Provider,gate:SafetyGate,context:ToolContext,approve:callback)`;
`async run(text:str) -> RunResult`.
`RunResult`: `text:str`, `stop_reason:str`, `steps:int`, `tool_calls:int`,
`usage:Usage`, `elapsed_seconds:float`, `turn_id:str`.
Stop reason: completed, max_steps, timeout, repeated_call, denied_repeat, budget,
provider_error, cancelled, local_limit.
`CommandRunner(loop)`; `async run(text)->RunResult` (sırala, aynı girişi kullan).

## Plugin ve kabul isimleri (tool/bench geliştiricileri)

`list_files(path: str|None=None)`, `read_file(path)`, `create_file(path,content)`,
`move_file(source,destination)`, `rename_file(path,new_name)`, `delete_file(path)`,
`create_directory(path)`, `open_application(name)`, `close_application(name)`,
`web_search(query)`, `open_url(url)`, `read_clipboard()`, `write_clipboard(text)`,
`system_info()`, `current_time()`.
list_files data.entries: [{name,path,size,is_dir}]; read_file data.content: str.
Plugin platform adaptörleri context.services üzerinden enjekte edilir:
`trash` callable(Path)->None; `web_search` async callable(str)->list[dict[str,str]].
İzole senaryolar gerçek OS trash yerine fixture çöp dizinine taşıma kullanır.

`bench/run.py --sahte` ağsız ve bütün senaryolarda gerçek AgentLoop/gate kullanır;
`--canli --model <config anahtarı veya OpenRouter katalog id>` ücretsizliği
doğruladıktan sonra kullanır. Canlı fixture araçları yalnız geçici masaüstü ve
sahte platform servislerine erişir. Yapılandırılan gerçek uygulamalar kullanılmaz.
