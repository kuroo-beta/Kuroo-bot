from storage.engine import CollectionStore, NOW
from kuroo.workflows.automod_config import default_config, config_from_record

CollectionName = 'automod'

_store = CollectionStore(
    name=CollectionName,
    defaults={'antilink_enabled': True, 'antilink_whitelist_roles': [], 'antilink_whitelist_channels': [], 'antispam_enabled': True, 'antispam_whitelist_roles': [], 'antispam_whitelist_channels': [], 'antispam_max_messages': 10, 'antispam_max_interval': 30, 'antispam_max_mentions': 5, 'antispam_max_emojis': 10, 'antispam_max_caps': 50, 'antispam_punishment': 'mute', 'antispam_punishment_duration': 10, 'antibadwords_enabled': True, 'antibadwords_whitelist_roles': [], 'antibadwords_whitelist_channels': [], 'antibadwords_words': [], 'created_at': NOW, 'native_config': default_config(), 'native_rule_ids': {}, 'native_status': {}, 'native_pending': []},
    unique_sets=[['guild_id']],
    json_fields=set(['antibadwords_whitelist_channels', 'antibadwords_whitelist_roles', 'antibadwords_words', 'antilink_whitelist_channels', 'antilink_whitelist_roles', 'antispam_whitelist_channels', 'antispam_whitelist_roles']),
    datetime_fields=set(['created_at']),
    sequence_fields={},
    update_cache=('automod_cache', ['guild_id']),
    delete_cache=('automod_cache', ['guild_id']),
)

async def create_table():
    return await _store.prepare()

async def insert(

    id:int=None,
    guild_id:int=None,
    antilink_enabled:bool=None,
    antilink_rule_id:int=None,
    antilink_whitelist_roles:list=None,
    antilink_whitelist_channels:list=None,
    antispam_enabled:bool=None,
    antispam_whitelist_roles:list=None,
    antispam_whitelist_channels:list=None,
    antispam_max_messages:int=None,
    antispam_max_interval:int=None,
    antispam_max_mentions:int=None,
    antispam_max_emojis:int=None,
    antispam_max_caps:int=None,
    antispam_punishment:str=None,
    antispam_punishment_duration:int=None,
    antibadwords_enabled:bool=None,
    antibadwords_rule_id:int=None,
    antibadwords_whitelist_roles:list=None,
    antibadwords_whitelist_channels:list=None,
    antibadwords_words:list=None,
    created_at:str=None,
    native_config:dict=None,
    native_rule_ids:dict=None,
    native_status:dict=None,
    native_pending:list=None
):
    payload = locals()
    if native_config is None and any(value is not None for name, value in payload.items()
                                     if name.startswith(('antilink_', 'antispam_', 'antibadwords_'))):
        legacy = {**_store.defaults, **{key: value for key, value in payload.items() if value is not None}}
        legacy['native_config'] = None
        payload['native_config'] = config_from_record(legacy)
    return await _store.insert(payload)

async def update(

    id:int,
    guild_id:int=None,
    antilink_enabled:bool=None,
    antilink_rule_id:int=None,
    antilink_whitelist_roles:list=None,
    antilink_whitelist_channels:list=None,
    antispam_enabled:bool=None,
    antispam_whitelist_roles:list=None,
    antispam_whitelist_channels:list=None,
    antispam_max_messages:int=None,
    antispam_max_interval:int=None,
    antispam_max_mentions:int=None,
    antispam_max_emojis:int=None,
    antispam_max_caps:int=None,
    antispam_punishment:str=None,
    antispam_punishment_duration:int=None,
    antibadwords_enabled:bool=None,
    antibadwords_rule_id:int=None,
    antibadwords_whitelist_roles:list=None,
    antibadwords_whitelist_channels:list=None,
    antibadwords_words:list=None,
    created_at:str=None,
    native_config:dict=None,
    native_rule_ids:dict=None,
    native_status:dict=None,
    native_pending:list=None
):
    return await _store.update(locals())

async def get(

    id:int=None,
    guild_id:int=None,
    antilink_enabled:bool=None,
    antilink_rule_id:int=None,
    antilink_whitelist_roles:list=None,
    antilink_whitelist_channels:list=None,
    antispam_enabled:bool=None,
    antispam_whitelist_roles:list=None,
    antispam_whitelist_channels:list=None,
    antispam_max_messages:int=None,
    antispam_max_interval:int=None,
    antispam_max_mentions:int=None,
    antispam_max_emojis:int=None,
    antispam_max_caps:int=None,
    antispam_punishment:str=None,
    antispam_punishment_duration:int=None,
    antibadwords_enabled:bool=None,
    antibadwords_rule_id:int=None,
    antibadwords_whitelist_roles:list=None,
    antibadwords_whitelist_channels:list=None,
    antibadwords_words:list=None,
    created_at:str=None
):
    return await _store.get(locals())

async def gets(

    id:int=None,
    guild_id:int=None,
    antilink_enabled:bool=None,
    antilink_rule_id:int=None,
    antilink_whitelist_roles:list=None,
    antilink_whitelist_channels:list=None,
    antispam_enabled:bool=None,
    antispam_whitelist_roles:list=None,
    antispam_whitelist_channels:list=None,
    antispam_max_messages:int=None,
    antispam_max_interval:int=None,
    antispam_max_mentions:int=None,
    antispam_max_emojis:int=None,
    antispam_max_caps:int=None,
    antispam_punishment:str=None,
    antispam_punishment_duration:int=None,
    antibadwords_enabled:bool=None,
    antibadwords_rule_id:int=None,
    antibadwords_whitelist_roles:list=None,
    antibadwords_whitelist_channels:list=None,
    antibadwords_words:list=None,
    created_at:str=None
):
    return await _store.gets(locals())

async def delete(

    id:int=None,
    guild_id:int=None,
    antilink_enabled:bool=None,
    antilink_rule_id:int=None,
    antilink_whitelist_roles:list=None,
    antilink_whitelist_channels:list=None,
    antispam_enabled:bool=None,
    antispam_whitelist_roles:list=None,
    antispam_whitelist_channels:list=None,
    antispam_max_messages:int=None,
    antispam_max_interval:int=None,
    antispam_max_mentions:int=None,
    antispam_max_emojis:int=None,
    antispam_max_caps:int=None,
    antispam_punishment:str=None,
    antispam_punishment_duration:int=None,
    antibadwords_enabled:bool=None,
    antibadwords_rule_id:int=None,
    antibadwords_whitelist_roles:list=None,
    antibadwords_whitelist_channels:list=None,
    antibadwords_words:list=None,
    created_at:str=None
):
    return await _store.delete(locals())

async def get_all():
    return await _store.get_all()

