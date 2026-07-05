# ZayderFlow

Editor visual low-code de arquiteturas multi-agente de IA. Desenhe caixinhas
(personagens com modelo e personalidade próprios), arraste setas entre elas, e
rode o fluxo vendo as mensagens fluírem ao vivo entre os agentes.

Irmão do [Zayder](https://github.com/Andrade020/Zayder): mesma filosofia —
local, offline, HTML único sem build, modelos baratos — mas aqui **o fluxo é
seu**. Painel de 5 analistas com um crítico e um elogiador, hierarquia
gerente→subagentes, pipeline arquiteto→codador→revisor: o que você desenhar,
ele executa.

## Instalação

```
pip install zayderflow
```

(ou `uv tool install zayderflow` / `pipx install zayderflow`)

## Uso

```
zflow
```

Abre a UI no navegador (porta 8430). Aí:

1. **Desenhe** — adicione agentes de texto 🧠 (pensam e escrevem) ou codadores
   🛠️ (editam arquivos do projeto via Aider), e arraste setas do círculo
   direito de um nó até outro nó. Ou carregue um 📚 template pronto.
2. **Configure** — clique num agente: nome, modelo (formato litellm, ex.
   `deepseek/deepseek-v4-flash`), personalidade por chips (rigoroso, crítico,
   elogiador, cético…) e instruções extras. O prompt final é visível.
3. **Rode** — digite a tarefa e aperte ▶. Os nós acendem enquanto pensam, as
   setas pulsam quando a mensagem passa, e cada resposta fica a um clique.

O grafo é salvo automaticamente em `.zflow/graph.json` no diretório do projeto.

### Chaves de API

Ficam em `~/.zflow/keys.json` (nunca no repositório) ou no ambiente
(`DEEPSEEK_API_KEY` etc.). Quem já usa o Zayder não precisa configurar nada:
o ZayderFlow lê `~/.zayder/keys.json` como fallback.

### Sem UI (scripts / CI)

```
zflow run --task "avalie esta ideia: ..." --graph meu-fluxo.json --yes
```

## Como funciona

- O grafo é um DAG interpretado por níveis topológicos: agentes de texto do
  mesmo nível rodam **em paralelo**; codadores são serializados (dois Aiders no
  mesmo repo corrompem o git) e pedem sua aprovação antes de editar arquivos.
- Cada nó recebe a tarefa original (opcional) + as saídas dos predecessores.
- Custo por nó medido na hora (tokens da resposta × tabela do provedor), com
  teto de custo por execução e o selo "economizou vs Claude Opus".
- Ciclos (ex. algoritmo evolutivo com N gerações) ainda não são suportados; o
  template evolutivo vem "desenrolado" em 2 gerações. O formato do grafo já
  reserva os campos para isso.

## Desenvolvimento

```
uv venv --python 3.12 && uv pip install -e ".[dev]"
.venv/Scripts/python -m pytest
```
