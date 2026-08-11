"""
MB Menu Diff — Daily checker
Checks Toast /metadata endpoint for menu changes, pulls full menu if changed,
runs diff against last snapshot, posts to Slack if differences found.
"""

import os
import json
import requests
from datetime import datetime, timezone

# ── Config from environment / GitHub Secrets ──────────────────────────────
TOAST_CLIENT_ID      = os.environ['TOAST_CLIENT_ID']
TOAST_CLIENT_SECRET  = os.environ['TOAST_CLIENT_SECRET']
TOAST_RESTAURANT_GUID = os.environ['TOAST_RESTAURANT_GUID']
SLACK_WEBHOOK_URL    = os.environ['SLACK_WEBHOOK_URL']
SNAPSHOT_PATH        = 'snapshot.json'   # stored in mb-menu-snapshots repo

TOAST_BASE           = 'https://ws-api.toasttab.com'
OWNER_MENU_GUID      = 'cf9db3d5-c804-4441-875d-f998f66689ef'

# ── Toast auth ────────────────────────────────────────────────────────────

def get_toast_token():
    resp = requests.post(
        f'{TOAST_BASE}/authentication/v1/authentication/login',
        json={
            'clientId': TOAST_CLIENT_ID,
            'clientSecret': TOAST_CLIENT_SECRET,
            'userAccessType': 'TOAST_MACHINE_CLIENT',
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()['token']['accessToken']

def toast_headers(token):
    return {
        'Authorization': f'Bearer {token}',
        'Toast-Restaurant-External-ID': TOAST_RESTAURANT_GUID,
    }

# ── Toast API calls ───────────────────────────────────────────────────────

def get_menu_last_updated(token):
    resp = requests.get(
        f'{TOAST_BASE}/menus/v3/metadata',
        headers=toast_headers(token),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get('lastUpdated', '')

def get_full_menu(token):
    resp = requests.get(
        f'{TOAST_BASE}/menus/v3/menus',
        headers=toast_headers(token),
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()

# ── Snapshot helpers ──────────────────────────────────────────────────────

def load_snapshot():
    if os.path.exists(SNAPSHOT_PATH):
        with open(SNAPSHOT_PATH) as f:
            return json.load(f)
    return None

def save_snapshot(data):
    with open(SNAPSHOT_PATH, 'w') as f:
        json.dump(data, f, indent=2)

# ── Menu parsing ──────────────────────────────────────────────────────────

def get_owner_menu(data):
    for m in data.get('menus', []):
        if m.get('guid') == OWNER_MENU_GUID or 'Owner' in m.get('name', ''):
            return m
    return None

def get_mod_groups(item, mg_ref, mo_ref):
    groups = []
    for ref_id in item.get('modifierGroupReferences', []):
        mg = mg_ref.get(str(ref_id), {})
        if not mg:
            continue
        options = []
        for opt_id in mg.get('modifierOptionReferences', []):
            opt = mo_ref.get(str(opt_id), {})
            options.append({'name': opt.get('name', ''), 'guid': opt.get('guid', '')})
        groups.append({
            'name': mg.get('name', ''),
            'guid': mg.get('guid', ''),
            'required': mg.get('requiredMode', ''),
            'min': mg.get('minSelections'),
            'max': mg.get('maxSelections'),
            'options': options,
        })
    return groups

def flat_items(menu, mg_ref, mo_ref):
    items = {}
    for g in menu.get('menuGroups', []):
        for item in g.get('menuItems', []):
            items[item['name']] = {
                'group': g['name'],
                'guid': item.get('guid', ''),
                'price': item.get('price', 0),
                'mgs': get_mod_groups(item, mg_ref, mo_ref),
            }
    return items

def guid_to_name_map(menu):
    result = {}
    for g in menu.get('menuGroups', []):
        for item in g.get('menuItems', []):
            if item.get('guid'):
                result[item['guid']] = item['name']
    return result

# ── Diff logic ────────────────────────────────────────────────────────────

def compute_diff(prev_data, curr_data):
    mg_prev = prev_data.get('modifierGroupReferences', {})
    mo_prev = prev_data.get('modifierOptionReferences', {})
    mg_curr = curr_data.get('modifierGroupReferences', {})
    mo_curr = curr_data.get('modifierOptionReferences', {})

    m_prev = get_owner_menu(prev_data)
    m_curr = get_owner_menu(curr_data)
    if not m_prev or not m_curr:
        return []

    prev_items   = flat_items(m_prev, mg_prev, mo_prev)
    curr_items   = flat_items(m_curr, mg_curr, mo_curr)
    prev_gtn     = guid_to_name_map(m_prev)
    curr_gtn     = guid_to_name_map(m_curr)
    prev_groups  = set(g['name'] for g in m_prev.get('menuGroups', []))
    curr_groups  = set(g['name'] for g in m_curr.get('menuGroups', []))

    changes = []

    # Group changes
    for g in sorted(curr_groups - prev_groups):
        changes.append(('Group Added', f'"{g}"', ''))
    for g in sorted(prev_groups - curr_groups):
        changes.append(('Group Removed', f'"{g}"', ''))

    # Item name string changes (same GUID)
    for guid in sorted(set(prev_gtn) & set(curr_gtn)):
        if prev_gtn[guid] != curr_gtn[guid]:
            changes.append(('Item Name String Changed', curr_gtn[guid],
                f'"{prev_gtn[guid]}" → "{curr_gtn[guid]}"'))

    # Items added / removed
    for iname in sorted(set(curr_items) - set(prev_items)):
        d = curr_items[iname]
        if d['guid'] not in prev_gtn:
            mg_names = [mg['name'] for mg in d['mgs']]
            changes.append(('Item Added', iname,
                f"Group: {d['group']} | ${d['price']:.2f}" +
                (f" | Mods: {', '.join(mg_names)}" if mg_names else '')))
    for iname in sorted(set(prev_items) - set(curr_items)):
        d = prev_items[iname]
        if d['guid'] not in curr_gtn:
            changes.append(('Item Removed', iname, f"Was in: {d['group']}"))

    # Item moved
    for iname in sorted(set(curr_items) & set(prev_items)):
        if curr_items[iname]['group'] != prev_items[iname]['group']:
            changes.append(('Item Moved', iname,
                f"{prev_items[iname]['group']} → {curr_items[iname]['group']}"))

    # Price change
    for iname in sorted(set(curr_items) & set(prev_items)):
        if curr_items[iname]['price'] != prev_items[iname]['price']:
            changes.append(('Price Change', iname,
                f"${prev_items[iname]['price']:.2f} → ${curr_items[iname]['price']:.2f}"))

    # Modifier changes
    for iname in sorted(set(curr_items) & set(prev_items)):
        prev_mgs = {mg['name']: mg for mg in prev_items[iname]['mgs']}
        curr_mgs = {mg['name']: mg for mg in curr_items[iname]['mgs']}
        prev_mgs_by_guid = {mg['guid']: mg for mg in prev_items[iname]['mgs'] if mg['guid']}
        curr_mgs_by_guid = {mg['guid']: mg for mg in curr_items[iname]['mgs'] if mg['guid']}

        for mgname in sorted(set(curr_mgs) - set(prev_mgs)):
            opts = [o['name'] for o in curr_mgs[mgname]['options']]
            req = 'REQUIRED' if curr_mgs[mgname]['required'] == 'REQUIRED' else 'optional'
            min_max = f" | min:{curr_mgs[mgname]['min']} max:{curr_mgs[mgname]['max']}" if (curr_mgs[mgname]['min'] is not None or curr_mgs[mgname]['max'] is not None) else ''
            changes.append(('Modifier Group Added', iname,
                f'"{mgname}" [{req}]{min_max} | Options: {", ".join(opts)}'))
        for mgname in sorted(set(prev_mgs) - set(curr_mgs)):
            req = 'REQUIRED' if prev_mgs[mgname]['required'] == 'REQUIRED' else 'optional'
            changes.append(('Modifier Group Removed', iname, f'"{mgname}" [{req}]'))

        # Modifier group name string changed (same guid)
        for guid in sorted(set(prev_mgs_by_guid) & set(curr_mgs_by_guid)):
            if prev_mgs_by_guid[guid]['name'] != curr_mgs_by_guid[guid]['name']:
                changes.append(('Modifier Group Name Changed', iname,
                    f'"{prev_mgs_by_guid[guid]["name"]}" → "{curr_mgs_by_guid[guid]["name"]}"'))

        for mgname in sorted(set(prev_mgs) & set(curr_mgs)):
            pm, cm = prev_mgs[mgname], curr_mgs[mgname]
            pfx = f'"{mgname}"'

            if pm['required'] != cm['required']:
                prev_req = 'REQUIRED' if pm['required'] == 'REQUIRED' else 'optional'
                curr_req = 'REQUIRED' if cm['required'] == 'REQUIRED' else 'optional'
                changes.append(('Modifier Required Flag Changed', iname,
                    f'{pfx} | was {prev_req} → now {curr_req}'))
            if pm['min'] != cm['min'] or pm['max'] != cm['max']:
                changes.append(('Modifier Min/Max Changed', iname,
                    f'{pfx} | min: {pm["min"]} → {cm["min"]} | max: {pm["max"]} → {cm["max"]}'))

            prev_opts_by_guid = {o['guid']: o['name'] for o in pm['options'] if o['guid']}
            curr_opts_by_guid = {o['guid']: o['name'] for o in cm['options'] if o['guid']}
            for og in sorted(set(prev_opts_by_guid) & set(curr_opts_by_guid)):
                if prev_opts_by_guid[og] != curr_opts_by_guid[og]:
                    changes.append(('Modifier Option Name Changed', iname,
                        f'{pfx} | "{prev_opts_by_guid[og]}" → "{curr_opts_by_guid[og]}"'))

            prev_opt_names = set(o['name'] for o in pm['options'])
            curr_opt_names = set(o['name'] for o in cm['options'])
            for oname in sorted(curr_opt_names - prev_opt_names):
                changes.append(('Modifier Option Added', iname, f'{pfx} | "{oname}"'))
            for oname in sorted(prev_opt_names - curr_opt_names):
                changes.append(('Modifier Option Removed', iname, f'{pfx} | "{oname}"'))

    return changes

# ── Slack formatting ──────────────────────────────────────────────────────

TYPE_EMOJI = {
    'Item Added':                    '➕',
    'Item Removed':                  '➖',
    'Item Moved':                    '↔️',
    'Item Name String Changed':      '🔤',
    'Price Change':                  '💰',
    'Group Added':                   '📂',
    'Group Removed':                 '🗂️',
    'Modifier Group Added':          '➕',
    'Modifier Group Removed':        '➖',
    'Modifier Group Name Changed':   '🔤',
    'Modifier Required Flag Changed':'⚙️',
    'Modifier Min/Max Changed':      '⚙️',
    'Modifier Option Added':         '➕',
    'Modifier Option Removed':       '➖',
    'Modifier Option Name Changed':  '🔤',
}

SLACK_MENTIONS = ' '.join([
    '<@U09AWCWQBGA>',  # Haroon
    '<@U1F9S04BA>',    # Hillary
    '<@U0B98MN84H3>',  # Charles
    '<@U02UCUG5ANN>',  # Mona
    '<@U049TGW0FKP>',  # Jon
    '<@U0155GU3G86>',  # Mohan
])

def format_slack_message(changes, check_time):
    now_str = check_time.strftime('%B %-d, %Y at %-I:%M %p PT')
    count = len(changes)

    lines = [
        f'🍞 *Lansdale Menu Update — {now_str}*',
        SLACK_MENTIONS,
        f'*{count} change{"s" if count != 1 else ""} detected on the *Owner / Otter Menu*\n',
    ]

    # Group by change type for readability
    from collections import defaultdict
    by_type = defaultdict(list)
    for change_type, item, detail in changes:
        by_type[change_type].append((item, detail))

    type_order = [
        'Item Added', 'Item Removed', 'Item Moved', 'Item Name String Changed', 'Price Change',
        'Group Added', 'Group Removed',
        'Modifier Group Added', 'Modifier Group Removed', 'Modifier Group Name Changed',
        'Modifier Required Flag Changed', 'Modifier Min/Max Changed',
        'Modifier Option Added', 'Modifier Option Removed', 'Modifier Option Name Changed',
    ]

    for t in type_order:
        if t not in by_type:
            continue
        emoji = TYPE_EMOJI.get(t, '•')
        lines.append(f'*{emoji} {t}* ({len(by_type[t])})')
        for item, detail in by_type[t]:
            # Highlight REQUIRED and optional in the detail line
            formatted_detail = detail.replace('[REQUIRED]', '*[REQUIRED]*').replace('[optional]', '_[optional]_')
            formatted_detail = formatted_detail.replace('now REQUIRED', '*now REQUIRED*').replace('now optional', '_now optional_')
            lines.append(f'  • `{item}`' + (f'\n    {formatted_detail}' if formatted_detail else ''))
        lines.append('')

    lines.append(f'_Posted to #menu-update-notification_\n<https://hgreene-presto.github.io/mb-menu-tools|View full diff tool>')

    return '\n'.join(lines)

def post_to_slack(message):
    resp = requests.post(
        SLACK_WEBHOOK_URL,
        json={'text': message},
        timeout=30,
    )
    resp.raise_for_status()
    print('Posted to Slack.')

# ── Main ──────────────────────────────────────────────────────────────────

def main():
    check_time_utc = datetime.now(timezone.utc)
    # Convert to PT for display (UTC-7 PDT / UTC-8 PST — simplified)
    from datetime import timedelta
    check_time_pt = check_time_utc - timedelta(hours=7)

    print(f'[{check_time_utc.isoformat()}] Starting menu check...')

    token = get_toast_token()
    print('Toast token obtained.')

    last_updated = get_menu_last_updated(token)
    print(f'Menu lastUpdated: {last_updated}')

    snapshot = load_snapshot()
    prev_last_updated = snapshot.get('lastUpdated', '') if snapshot else ''

    if last_updated == prev_last_updated:
        print('No change in lastUpdated timestamp. Skipping full pull.')
        return

    print(f'Timestamp changed ({prev_last_updated} → {last_updated}). Pulling full menu...')
    curr_data = get_full_menu(token)
    curr_data['lastUpdated'] = last_updated
    print('Full menu pulled.')

    if snapshot is None:
        print('No previous snapshot found. Saving current as baseline.')
        save_snapshot(curr_data)
        return

    changes = compute_diff(snapshot, curr_data)
    print(f'{len(changes)} changes found.')

    if changes:
        message = format_slack_message(changes, check_time_pt)
        post_to_slack(message)
    else:
        print('Timestamp changed but no menu differences detected. Silent.')

    save_snapshot(curr_data)
    print('Snapshot updated.')

if __name__ == '__main__':
    main()
