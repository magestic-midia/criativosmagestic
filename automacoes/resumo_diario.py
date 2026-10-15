# Resumo diário Magestic: anomalias, ritmo de gasto e status das contas (dados de ontem).
# Uso: python3 resumo_diario.py  -> grava resumo_diario.md e resumo_diario.json
import json, os, time, datetime, urllib.request, urllib.parse, urllib.error
BASE = os.path.dirname(os.path.abspath(__file__))
T = os.environ.get('META_TOKEN', '').strip() or open(os.path.join(BASE, '..', 'meta_token.txt')).read().strip()
SAIDA = os.environ.get('SAIDA_DIR', BASE)
V = 'v21.0'

def get(path, **q):
    q['access_token'] = T
    url = f'https://graph.facebook.com/{V}/{path}?' + urllib.parse.urlencode(q)
    for tent in range(4):
        try:
            return json.load(urllib.request.urlopen(url, timeout=90))
        except urllib.error.HTTPError as e:
            try: err = json.load(e).get('error', {})
            except Exception: err = {}
            if err.get('code') in (4, 17, 32, 613, 80004) or e.code >= 500:
                time.sleep(20 * (tent + 1)); continue
            return {'ERR': err.get('message', str(e))}
        except Exception:
            time.sleep(10)
    return {'ERR': 'falhou'}

def todos(r):
    out = []
    while 'ERR' not in r:
        out += r.get('data', [])
        nxt = r.get('paging', {}).get('next')
        if not nxt: break
        r = json.load(urllib.request.urlopen(nxt, timeout=90))
    return out

def conv(actions):
    for a in actions or []:
        if a['action_type'] == 'onsite_conversion.messaging_conversation_started_7d': return float(a['value'])
    return 0.0

STATUS = {2: 'desativada pela Meta', 3: 'pagamento pendente', 7: 'em análise de risco', 8: 'pagamento pendente de liquidação',
          9: 'em período de carência (pagamento)', 100: 'encerramento pendente', 101: 'encerrada'}
hoje = datetime.date.today()
ontem = hoje - datetime.timedelta(days=1)
ini = ontem - datetime.timedelta(days=7)
tr = json.dumps({'since': ini.isoformat(), 'until': ontem.isoformat()})
contas = todos(get('me/adaccounts', limit=50,
    fields=f'name,account_status,currency,insights.time_range({tr}).time_increment(1).limit(10){{spend,actions,date_start}}'))

alertas = {'conta': [], 'parada': [], 'ritmo': [], 'anomalia': [], 'reprovado': [], 'destaque': []}
tot = {'g_y': 0, 'c_y': 0, 'g_b': 0, 'c_b': 0}
ativas = 0
fmt = lambda v: ('R$ ' + f'{v:,.2f}').replace(',', 'X').replace('.', ',').replace('X', '.')
import threading
LOCK = threading.Lock(); cont = []
def analisa(c):
    nome, aid, moeda = c['name'].strip(), c['id'], c.get('currency', 'BRL')
    dias = {d['date_start']: (float(d.get('spend', 0)), conv(d.get('actions'))) for d in (c.get('insights') or {}).get('data', [])}
    g_y, c_y = dias.get(ontem.isoformat(), (0.0, 0.0))
    base = [v for k, v in dias.items() if k != ontem.isoformat()]
    g_b = sum(v[0] for v in base) / 7; c_b = sum(v[1] for v in base) / 7
    teve_gasto = g_y > 0 or g_b > 0
    # orçamento diário ativo
    adsets = todos(get(f'{aid}/adsets', limit=200, effective_status='["ACTIVE"]',
                       fields='daily_budget,lifetime_budget,campaign{id,daily_budget}'))
    camp, orc, vitalicio = {}, 0.0, False
    for s in adsets:
        cp = s.get('campaign') or {}
        if cp.get('daily_budget'): camp[cp['id']] = float(cp['daily_budget']) / 100
        elif s.get('daily_budget'): orc += float(s['daily_budget']) / 100
        elif s.get('lifetime_budget'): vitalicio = True
    orc += sum(camp.values())
    if not teve_gasto and orc == 0: return
    cont.append(1)
    st = c.get('account_status', 1)
    if st != 1:
        alertas['conta'].append(f'{nome}: conta {STATUS.get(st, "com restrição (status %s)" % st)}')
        return
    if moeda == 'BRL':
        with LOCK:
            tot['g_y'] += g_y; tot['c_y'] += c_y; tot['g_b'] += g_b; tot['c_b'] += c_b
    m = (lambda v: fmt(v)) if moeda == 'BRL' else (lambda v: f'{moeda} {v:.2f}')
    # ritmo
    if orc > 0 and not vitalicio:
        if g_y < 1:
            alertas['parada'].append(f'{nome}: orçamento ativo de {m(orc)}/dia, mas não gastou nada ontem')
        elif g_y < 0.5 * orc:
            alertas['ritmo'].append(f'{nome}: gastou {m(g_y)} de {m(orc)}/dia ({g_y/orc:.0%}), entregando abaixo')
        elif g_y > 1.75 * orc:
            alertas['ritmo'].append(f'{nome}: gastou {m(g_y)} com orçamento de {m(orc)}/dia ({g_y/orc:.0%}), acima do previsto')
    # anomalias
    if g_y >= 20 and c_y == 0 and c_b >= 1:
        alertas['anomalia'].append(f'{nome}: gastou {m(g_y)} e não gerou nenhuma conversa (média {c_b:.1f}/dia)')
    elif c_y >= 1 and c_b * 7 >= 5 and g_y >= 30:
        cpc_y, cpc_b = g_y / c_y, (g_b * 7) / (c_b * 7)
        if cpc_y >= 1.7 * cpc_b:
            alertas['anomalia'].append(f'{nome}: custo por conversa {m(cpc_y)}, {cpc_y/cpc_b-1:.0%} acima da média de 7 dias ({m(cpc_b)})')
        elif cpc_y <= 0.6 * cpc_b and c_y >= 5:
            alertas['destaque'].append(f'{nome}: custo por conversa caiu para {m(cpc_y)} (média {m(cpc_b)}). Vale ver o que funcionou')
    # anúncios reprovados ou com problema que deveriam estar rodando
    ads = todos(get(f'{aid}/ads', limit=100, effective_status='["DISAPPROVED","WITH_ISSUES"]', fields='name,status,effective_status,adset{effective_status},campaign{effective_status}'))
    ruins = [a for a in ads if a.get('status') == 'ACTIVE' and (a.get('adset') or {}).get('effective_status') == 'ACTIVE' and (a.get('campaign') or {}).get('effective_status') == 'ACTIVE']
    if ruins:
        n = ', '.join(a['name'] for a in ruins[:3]) + (f' e mais {len(ruins)-3}' if len(ruins) > 3 else '')
        alertas['reprovado'].append(f'{nome}: {len(ruins)} anúncio(s) reprovado(s) ou com problema ({n})')

from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(6) as ex: list(ex.map(analisa, contas))
ativas = len(cont)
for k in alertas: alertas[k].sort()
meses = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez']
L = [f'**Resumo Magestic, {ontem.day} de {meses[ontem.month-1]}** ({ativas} contas ativas)', '']
cy = tot['g_y'] / tot['c_y'] if tot['c_y'] else 0
cb = tot['g_b'] / tot['c_b'] if tot['c_b'] else 0
var = lambda a, b: (f' ({a/b-1:+.0%} vs média 7d)' if b else '')
L.append(f'Ontem: {fmt(tot["g_y"])} investidos{var(tot["g_y"], tot["g_b"])}, {int(tot["c_y"])} conversas{var(tot["c_y"], tot["c_b"])}, '
         f'{fmt(cy)} por conversa{var(cy, cb)}.')
blocos = [('🔴 Contas com problema', 'conta'), ('🔴 Paradas', 'parada'), ('🟠 Anúncios reprovados', 'reprovado'),
          ('🟠 Anomalias', 'anomalia'), ('🟡 Gasto fora do ritmo', 'ritmo'), ('🟢 Destaques', 'destaque')]
n_al = sum(len(v) for k, v in alertas.items() if k != 'destaque')
for titulo, k in blocos:
    if alertas[k]:
        L += ['', f'**{titulo}**'] + [f'- {x}' for x in alertas[k]]
if not alertas['ritmo'] and not alertas['parada'] and n_al:
    L += ['', '🟡 Gasto fora do ritmo: nenhuma conta fora do orçamento.']
if n_al == 0:
    L += ['', 'Nenhum alerta. Todas as contas dentro do esperado.']
L += ['', 'Painel completo: https://painel.magesticmidia.com.br']
open(os.path.join(SAIDA, 'resumo_diario.md'), 'w').write('\n'.join(L))
json.dump({'data': ontem.isoformat(), 'totais': tot, 'alertas': alertas}, open(os.path.join(SAIDA, 'resumo_diario.json'), 'w'), ensure_ascii=False, indent=1)
print('\n'.join(L))
