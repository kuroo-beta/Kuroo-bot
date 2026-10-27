"""Platform limits, defaults and payloads for the ten native AutoMod protections.

Native rules enforce messages/edits even while Kuroo is offline. Local checks only
supplement the spam, caps and emoji rules; they are not additional native rules.
"""
from copy import deepcopy
from datetime import timedelta
import re

import discord


# The platform supports six keyword rules and one of each remaining trigger.
RULES = {
    'links': ('Links', 1),
    'invites': ('Discord invites', 1),
    'words': ('Custom harassment words', 1),
    'scams': ('Nitro and credential scams', 1),
    'emoji': ('Excessive emoji', 1),
    'caps': ('All-caps messages', 1),
    'spam': ('Spam and message floods', 3),
    'language': ('Harmful language presets', 4),
    'mentions': ('Mention spam and raids', 5),
    'profiles': ('Member-profile impersonation', 6),
}
LEGACY_GROUPS = {
    'antilink': ('links', 'invites'),
    'antibadwords': ('words',),
    'antispam': ('spam', 'mentions', 'emoji', 'caps'),
}
WORD_RULES = ('words', 'scams', 'profiles')
DEFAULT_WORDS = {
    'words': ['kill yourself', 'kys'],
    'scams': ['*free nitro generator*', '*discord password*', '*scan this qr to claim nitro*'],
    'profiles': ['*discord staff*', '*discord support*', '*free nitro*'],
}
LIMITS = {
    'messages': (2, 30, 10), 'interval': (1, 120, 30),
    'mentions': (1, 50, 5), 'emoji': (2, 50, 10),
    'caps': (1, 100, 50), 'timeout': (1, 40320, 10),
}
LEGACY_LIMITS = {
    'messages': 'antispam_max_messages', 'interval': 'antispam_max_interval',
    'mentions': 'antispam_max_mentions', 'emoji': 'antispam_max_emojis',
    'caps': 'antispam_max_caps', 'timeout': 'antispam_punishment_duration',
}
CUSTOM_EMOJI = r'<a?:\w+:\d+>'
# Rust regex (Discord): includes common emoji, flags, symbols and keycaps.
EMOJI_UNIT = r'(?:<a?:\w+:\d+>|[\x{1F1E6}-\x{1F1FF}]{2}|[\x{1F300}-\x{1FAFF}\x{2600}-\x{27BF}]\x{FE0F}?(?:[\x{1F3FB}-\x{1F3FF}])?|[0-9#*]\x{FE0F}?\x{20E3})'
LOCAL_EMOJI = re.compile(CUSTOM_EMOJI + r'|[\U0001F1E6-\U0001F1FF]{2}|[\U0001F300-\U0001FAFF\u2600-\u27BF]\ufe0f?[\U0001F3FB-\U0001F3FF]?|[0-9#*]\ufe0f?\u20e3')


class ConfigError(ValueError):
    pass


def default_config():
    return {
        'version': 1,
        'rules': {key: {'enabled': True, 'words': deepcopy(DEFAULT_WORDS.get(key, [])),
                        'allow': [], 'roles': [], 'channels': []} for key in RULES},
        'limits': {key: spec[2] for key, spec in LIMITS.items()},
        'punishment': 'mute', 'alert_channel': None, 'presets': [1, 2, 3],
    }


def config_from_record(record):
    """Migrate without mistaking old false/default choices for permission to enable."""
    config = default_config()
    if record.get('native_config'):
        saved = record['native_config']
        for key in RULES:
            config['rules'][key].update(deepcopy(saved.get('rules', {}).get(key, {})))
        config['limits'].update(saved.get('limits', {}))
        for key in ('punishment', 'alert_channel', 'presets'):
            if key in saved:
                config[key] = deepcopy(saved[key])
    else:
        spam = bool(record.get('antispam_enabled', True))
        for rule in config['rules'].values():
            rule['enabled'] = spam
            rule['roles'] = list(record.get('antispam_whitelist_roles') or [])
            rule['channels'] = list(record.get('antispam_whitelist_channels') or [])
        for group, keys in LEGACY_GROUPS.items():
            for key in keys:
                rule = config['rules'][key]
                rule['enabled'] = bool(record.get(group + '_enabled', False))
                rule['roles'] = list(record.get(group + '_whitelist_roles') or [])
                rule['channels'] = list(record.get(group + '_whitelist_channels') or [])
        config['rules']['words']['words'] = [f'*{word}*' for word in record.get('antibadwords_words', [])]
        for key, field in LEGACY_LIMITS.items():
            low, high, default = LIMITS[key]
            try:
                config['limits'][key] = max(low, min(high, int(record.get(field, default))))
            except (ValueError, TypeError):
                config['limits'][key] = default
        punishment = record.get('antispam_punishment', 'mute')
        config['punishment'] = punishment if punishment in ('mute', 'warn', 'kick', 'ban', 'tempban') else 'warn'
    validate(config)
    return config


def validate(config):
    if set(config['rules']) != set(RULES):
        raise ConfigError('Exactly the ten supported AutoMod protections are required.')
    for key, rule in config['rules'].items():
        if not isinstance(rule['enabled'], bool):
            raise ConfigError(f'{key}: enabled must be true or false.')
        for field, maximum, length in (('words', 1000, 60), ('allow', 100, 60)):
            values = rule[field]
            if not isinstance(values, list) or len(values) > maximum or any(
                not isinstance(word, str) or not word.strip() or len(word) > length for word in values
            ):
                raise ConfigError(f'{key}: {field} requires at most {maximum} nonempty entries of at most {length} characters.')
        for field, maximum in (('roles', 20), ('channels', 50)):
            ids = rule[field]
            if not isinstance(ids, list) or len(ids) > maximum or any(type(sid) is not int or sid <= 0 for sid in ids):
                raise ConfigError(f'{key}: at most {maximum} valid {field} IDs are allowed.')
    for key, (low, high, _) in LIMITS.items():
        value = config['limits'][key]
        if type(value) is not int or not low <= value <= high:
            raise ConfigError(f'{key} must be between {low} and {high}.')
    if config['punishment'] not in ('mute', 'warn', 'kick', 'ban', 'tempban'):
        raise ConfigError('Punishment must be mute, warn, kick, ban or tempban.')
    if not config['presets'] or any(type(item) is not int or item not in (1, 2, 3) for item in config['presets']):
        raise ConfigError('Select one or more language presets: 1 profanity, 2 sexual content, 3 slurs.')
    if config['alert_channel'] is not None and (type(config['alert_channel']) is not int or config['alert_channel'] <= 0):
        raise ConfigError('Alert channel must be a valid channel ID.')


def rule_payload(key, config):
    """Compile platform-valid discord.py objects; no timeout on unsupported triggers."""
    rule = config['rules'][key]
    trigger_type = discord.AutoModRuleTriggerType(RULES[key][1])
    metadata = {}
    if key in WORD_RULES:
        if not rule['words']:
            raise ConfigError('needs configuration: add words with /automod words')
        metadata['keyword_filter'] = rule['words']
    elif key == 'links':
        metadata['regex_patterns'] = [r'(?i)(?:https?://|www\.)[^\s]+|\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\b(?:[/:?#][^\s]*)?']
    elif key == 'invites':
        metadata['regex_patterns'] = [r'(?i)(?:discord(?:app)?\.com/invite|discord\.gg|discord\.me)/[a-z0-9-]+']
    elif key == 'emoji':
        metadata['regex_patterns'] = [rf'(?:{EMOJI_UNIT}[^\S\r\n]*){{{config["limits"]["emoji"]},}}']
    elif key == 'caps':
        metadata['regex_patterns'] = [r'^(?:[^\p{L}]*\p{Lu}){8,}[^\p{L}]*$']
    elif key == 'language':
        metadata['presets'] = discord.AutoModPresets(profanity=1 in config['presets'],
                                                   sexual_content=2 in config['presets'], slurs=3 in config['presets'])
    elif key == 'mentions':
        metadata.update(mention_limit=config['limits']['mentions'], mention_raid_protection=True)
    if trigger_type.value in (1, 4, 6):
        metadata['allow_list'] = rule['allow']
    trigger = discord.AutoModTrigger(type=trigger_type, **metadata)
    actions = [discord.AutoModRuleAction(type=(discord.AutoModRuleActionType.block_member_interactions
                                              if key == 'profiles' else discord.AutoModRuleActionType.block_message))]
    if config['alert_channel']:
        actions.append(discord.AutoModRuleAction(channel_id=config['alert_channel']))
    if trigger_type.value in (1, 5) and config['punishment'] == 'mute':
        actions.append(discord.AutoModRuleAction(duration=timedelta(minutes=config['limits']['timeout'])))
    return dict(name='Kuroo AutoMod: ' + key,
                event_type=(discord.AutoModRuleEventType.member_update if key == 'profiles'
                            else discord.AutoModRuleEventType.message_send),
                trigger=trigger, actions=actions, enabled=rule['enabled'],
                exempt_roles=[discord.Object(id=sid) for sid in rule['roles']],
                exempt_channels=[discord.Object(id=sid) for sid in rule['channels']])


def fingerprint(payload):
    return (payload['name'], payload['event_type'].value, payload['trigger'].to_metadata_dict(),
            [action.to_dict() for action in payload['actions']], payload['enabled'],
            sorted(sid.id for sid in payload['exempt_roles']), sorted(sid.id for sid in payload['exempt_channels']))


def native_fingerprint(rule):
    return (rule.name, rule.event_type.value, rule.trigger.to_metadata_dict(),
            [action.to_dict() for action in rule.actions], rule.enabled,
            sorted(rule.exempt_role_ids), sorted(rule.exempt_channel_ids))


def keyword_matches(word, text):
    start = '' if word.startswith('*') else r'(?<!\w)'
    end = '' if word.endswith('*') else r'(?!\w)'
    return re.search(start + re.escape(word.strip('*')) + end, text, re.IGNORECASE) is not None


def emoji_count(text):
    """Treat modifier, flag and ZWJ sequences as one visible emoji locally."""
    total, previous_end = 0, None
    for match in LOCAL_EMOJI.finditer(text):
        if previous_end is None or text[previous_end:match.start()] != '\u200d':
            total += 1
        previous_end = match.end()
    return total
