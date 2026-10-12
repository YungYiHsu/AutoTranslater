"""Shared model picker rules for webnovel and ebook modes."""


def model_choices(catalog):
    return tuple(dict.fromkeys(m['model'] for m in catalog if m.get('model') and m['model'] != 'default'))


def supported_efforts(catalog, model):
    entries = [m for m in catalog if m['model'] == model]
    supported = [{e['reasoningEffort'] for e in m.get('supportedReasoningEfforts', [])} for m in entries]
    common = set.intersection(*supported) if supported else set()
    return tuple(e for e in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra') if e in common)
