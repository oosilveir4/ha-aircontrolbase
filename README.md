# AirControlBase (CCM21) — Home Assistant Custom Integration

Integração nativa para o **AirControlBase / CCM21** (gateway centralizado de ar-condicionado). Substitui Node-RED + MQTT por uma integração de primeira classe com descoberta automática de ACs, controle de clima e bloqueios.

> Logbook, histórico e agendamento são feitos pelas próprias funcionalidades nativas do Home Assistant (Logbook, History, Automations, Scripts) — esta integração foca em expor os ACs como entidades reais.

## Recursos

- **Climate** por AC — on/off, modos (cool, heat, auto, dry, fan_only), velocidade do vento (low/mid/high/auto), temperatura alvo (17–30 °C), temperatura atual.
- **Sensores de resumo** — ACs em cada modo (cool/heat/fan/stop/lock/error), total.
- **Código de erro por aparelho** — sensor de diagnóstico: `OK` quando a API retorna `error: "n"`, ou o código bruto (por exemplo `E0`).
- **Switches de lock** por AC — bloquear controle remoto, modo, modo cool, modo heat, ventilação.
- **Serviço** `aircontrolbase.refresh` — força poll imediato.
- **Auto re-login** — detecta `code: 40018` e refaz sessão automaticamente.
- **Polling** — 20 s.
- **i18n** — interface em pt-BR e en, com fan modes localizados ("mid" → "Médio" / "Medium").

## Instalação

### Via HACS (Custom Repository)

1. HACS → Integrações → menu (⋮) → **Custom repositories**
2. URL: `https://github.com/henr1quess30/ha-aircontrolbase`
3. Category: **Integration** → Add
4. Procure "AirControlBase (CCM21)" e clique **Download**
5. Reinicie o Home Assistant
6. Configurações → Dispositivos e Serviços → **Adicionar Integração** → "AirControlBase"

### Manual

1. Copie `custom_components/aircontrolbase/` para `<config>/custom_components/aircontrolbase/`.
2. Reinicie o Home Assistant.
3. Settings → Devices & Services → Add Integration → "AirControlBase".

## Configuração

Na adição da integração:

- **E-mail / conta**: o mesmo usado no [aircontrolbase.com](https://www.aircontrolbase.com).
- **Senha**: idem.

Se o `userId` não for detectado automaticamente do login, a integração pedirá manualmente. Para encontrá-lo:

1. Faça login no aircontrolbase.com.
2. Abra DevTools (F12) → aba **Network**.
3. Clique em qualquer página (ex.: "Controle de dispositivos").
4. Procure uma requisição `getDetails` → aba **Payload** → você verá `userId=<seu_id>`.

## Entidades criadas

Para cada AC:
- `climate.<nome_do_ac>`
- `switch.<nome_do_ac>_bloquear_controle_remoto`
- `switch.<nome_do_ac>_bloquear_modo`
- `switch.<nome_do_ac>_bloquear_modo_resfriar`
- `switch.<nome_do_ac>_bloquear_modo_aquecer`
- `switch.<nome_do_ac>_bloquear_ventilação`
- Sensor de diagnóstico **Código de erro** associado ao mesmo dispositivo.

Para o hub (1 vez):
- `sensor.acs_resfriando`
- `sensor.acs_aquecendo`
- `sensor.acs_em_ventilação`
- `sensor.acs_desligados`
- `sensor.acs_bloqueados`
- `sensor.acs_com_erro`
- `sensor.total_de_acs`

## Por que não tem histórico e agendamento?

O Home Assistant já oferece:

- **Logbook** + **History** — registra automaticamente todo comando e mudança de estado das entidades climate/switch.
- **Automations** e **Scripts** — substituem completamente o agendamento do AirControlBase, com mais flexibilidade (condições, gatilhos, integração com calendário, presença, etc.).

Manter essas features na integração seria duplicar o que o HA já faz melhor.

## Diagnóstico de falhas

O sensor **Código de erro** usa o campo `error` retornado para cada aparelho por
`/web/userGroup/getDetails`. O valor `n` é apresentado como `OK`; os demais códigos
são preservados, sem tradução de significado, pois dependem do modelo. Se o campo
estiver ausente ou vazio, o estado fica desconhecido. Aparelhos removidos ou falhas
na consulta tornam o sensor indisponível.

O portal também consulta o histórico em `/web/record/deviceLog`, com `type=m` e
paginação. Esse histórico não é consultado automaticamente pela integração; o
sensor representa o erro atual, atualizado a cada polling.

A resposta verificada não fornece uma lista separada de condensadoras. Portanto,
não são criadas entidades de unidades externas a partir de suposições. Conforme o
[manual do CCM21](https://carrierdobrasil.com.br/wp-content/uploads/2023/07/CCM-21_MIO-MControl-System-C-04-24-view.pdf),
em ECOSPLIT os códigos EA/EB/EC identificam a condensadora líder/escravas em falha;
o alarme detalhado deve ser verificado no display da unidade.

## Verificação das correções

Os testes usam Home Assistant 2026.11.2 com transporte HTTP simulado; não comandam
equipamentos reais:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
.venv/bin/ruff check custom_components/aircontrolbase tests
```

Os comandos de todas as entidades de uma conta compartilham uma fila com intervalo
mínimo de 0,8 s. Alterações confirmadas pela API são preservadas por até 40 s ao
montar comandos seguintes, para compensar o atraso de propagação; isso não é uma
confirmação de execução física. Falhas em grupos são informadas ao chamador após
tentar os demais membros. Grupos existentes migram para identificadores por conta
e área, preservando os `entity_id` já registrados quando há correspondência.

## Desenvolvimento

```
custom_components/aircontrolbase/
├── __init__.py        # setup/unload + registro do serviço refresh
├── api.py             # cliente HTTP async (aiohttp)
├── config_flow.py     # UI de login
├── const.py           # constantes e endpoints
├── coordinator.py     # DataUpdateCoordinator (poll 20s)
├── climate.py         # entidades climate por AC
├── sensor.py          # 7 sensores de resumo
├── switch.py          # 5 switches de lock por AC
├── services.py        # handler do refresh
├── services.yaml      # schema do serviço
├── manifest.json
└── translations/
    ├── en.json
    └── pt-BR.json
```

## Dashboard de exemplo

O arquivo [`examples/dashboard.yaml`](examples/dashboard.yaml) traz uma view pronta com seções "Grupos" (climates de área), sub-áreas internas e resumo. Como aplicar:

1. No HA: ⋮ no canto superior direito do dashboard → **Editar dashboard** → ⋮ → **Editor de configuração bruta**
2. Cole o conteúdo (substituindo ou como nova view)
3. Ajuste os `entity_id` se forem diferentes do seu setup

## Licença

MIT — veja `LICENSE`.
