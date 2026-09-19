# Apuração de Renda via Extratos PDF

![Python](https://img.shields.io/badge/Python-3.10+-blue?logo=python)
![Streamlit](https://img.shields.io/badge/Streamlit-1.62+-red?logo=streamlit)
![License](https://img.shields.io/badge/License-MIT-green)
![Status](https://img.shields.io/badge/Status-Produção-brightgreen)

Ferramenta profissional em Python + Streamlit para consolidar múltiplos extratos bancários em PDF, aplicar regras de negócio de inclusão/exclusão de renda e gerar automaticamente um relatório executivo em PDF com rastreabilidade completa.

---

## 🎯 Objetivo

A ferramenta permite que o usuário:

- Anexe múltiplos extratos bancários em PDF;
- Processe lançamentos financeiros automaticamente;
- Consolide entradas por mês;
- Exclua movimentações que não devem entrar na apuração de renda;
- Visualize uma prévia dos resultados na interface;
- Gere um relatório executivo em PDF para download.

---

## ✨ Funcionalidades

- Upload múltiplo de arquivos PDF;
- Extração de texto de PDFs nativos (PyMuPDF);
- Fallback com pdfplumber para tabelas complexas;
- Fallback com OCR (Tesseract) para PDFs escaneados;
- Integração opcional com Gemini AI para extração via IA;
- Parsing de datas e valores monetários no padrão brasileiro;
- **Detecção automática de 23+ bancos** (Nubank, Itaú, Bradesco, Santander, Caixa, BB, C6, Inter, PicPay, Sicoob, Sicredi, Will Bank, Neon, Mercado Pago, BTG, PagBank, Original, Safra, Banrisul, Pan, Crefisa, BV, HSBC);
- Motor de regras configurável para exclusão de renda (com fronteira de palavra para evitar falsos positivos);
- **Deduplicação automática de transações** em extratos com períodos sobrepostos;
- **Precisão monetária com Decimal** (sem erros de arredondamento IEEE 754);
- Revisão manual obrigatória antes da exportação;
- Consolidação mensal das entradas válidas;
- Cálculo de indicadores:
  - Total Geral Apurado;
  - Média Mensal Geral;
  - Média de Meses Completos;
- Tabela detalhada de entradas válidas agrupadas por mês;
- Tabela de auditoria com entradas excluídas e motivo;
- Geração de relatório executivo em PDF, Excel e CSV;
- **Interface profissional** com tema customizado, hero section, cards KPI e tabs organizadas;
- **Crédito de autoria** visível na interface, PDF, Excel, CSV e documentação.

---

## 🛠️ Stack Técnica

| Componente | Tecnologia | Versão |
|---|---|---|
| Interface | Streamlit | ≥ 1.62.0 |
| Dados | Pandas | ≥ 2.0.0 |
| Extração PDF | PyMuPDF (fitz) | ≥ 1.23.0 |
| Extração PDF | pdfplumber | ≥ 0.10.0 |
| OCR | Tesseract + pytesseract | ≥ 5.5.0 |
| Imagens | Pillow | ≥ 10.0.0 |
| Relatórios PDF | ReportLab | ≥ 4.0.0 |
| Excel | openpyxl | ≥ 3.1.0 |
| Datas | python-dateutil | ≥ 2.8.2 |
| IA (opcional) | google-genai | ≥ 1.0.0 |
| Variáveis de ambiente | python-dotenv | ≥ 1.0.0 |
| Testes | pytest | ≥ 8.0.0 |

---

## 📂 Estrutura do Projeto

```text
apuracao-renda-extratos-LANA/
├── app.py                      # Aplicação principal Streamlit
├── requirements.txt            # Dependências Python
├── packages.txt                # Dependências apt (Streamlit Cloud)
├── README.md                   # Documentação do projeto
├── .gitignore                  # Arquivos ignorados pelo Git
├── instalar.bat                # Script de instalação Windows
├── executar.bat                # Script de execução Windows
├── .streamlit/
│   ├── config.toml             # Tema visual da aplicação
│   └── style.css               # CSS customizado
├── config/
│   ├── exclusion_keywords.json # Palavras-chave para exclusão de renda
│   └── bank_markers.json       # Marcadores de bancos (opcional)
├── src/
│   ├── __init__.py
│   ├── pdf_extractor.py        # Extração de texto dos PDFs
│   ├── bank_detector.py        # Detecção automática do banco
│   ├── transaction_parser.py   # Interpretação das transações
│   ├── rules_engine.py         # Regras de inclusão/exclusão de renda
│   ├── income_calculator.py    # Cálculos e consolidações
│   ├── report_generator.py     # Geração do relatório PDF/Excel/CSV
│   └── gemini_extractor.py     # Extração via IA (Gemini)
└── tests/
    └── test_rules_engine.py    # Testes unitários das regras
```

---

## ▶️ Como Rodar Localmente

### 1. Clonar o repositório

```bash
git clone https://github.com/sunstrix/apuracao-renda-extratos-LANA.git
cd apuracao-renda-extratos-LANA
```

### 2. Instalação automatizada (Windows)

```powershell
.\instalar.bat
.\executar.bat
```

O script `instalar.bat` detecta/instala Python 3.11 ou 3.12, cria o ambiente virtual, instala dependências e o Tesseract OCR. O `executar.bat` ativa o venv e inicia o Streamlit.

### 3. Instalação manual

#### Criar ambiente virtual

```bash
python -m venv .venv
```

#### Ativar o ambiente virtual

**Windows (PowerShell):**
```powershell
.venv\Scripts\Activate.ps1
```

Se houver bloqueio de execução de scripts:
```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

**Linux/Mac:**
```bash
source .venv/bin/activate
```

#### Instalar as dependências

```bash
pip install -r requirements.txt
```

#### Executar a aplicação

```bash
streamlit run app.py
```

A aplicação abrirá automaticamente no navegador, normalmente em: `http://localhost:8501`

---

## 🔎 OCR para PDFs Escaneados

A aplicação tenta primeiro extrair a camada de texto do PDF. Se o PDF for uma imagem escaneada, ela usa OCR como fallback automático.

### Instalação do Tesseract OCR

**Windows (via Winget):**
```powershell
winget install UB-Mannheim.TesseractOCR
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt install tesseract-ocr tesseract-ocr-por tesseract-ocr-eng
```

**Mac (via Homebrew):**
```bash
brew install tesseract tesseract-lang
```

Após instalar, verifique se o executável `tesseract` está acessível no PATH:
```bash
tesseract --version
```

> **Observação importante:** PDFs nativos com camada textual são muito mais rápidos e confiáveis do que PDFs escaneados.

---

## 🧪 Testes

Para executar os testes unitários:
```bash
pytest tests/
```

Com verbosidade:
```bash
pytest tests/ -v
```

---

## ⚙️ Configuração das Regras de Exclusão

As palavras-chave usadas para excluir lançamentos da apuração de renda ficam no arquivo `config/exclusion_keywords.json`.

As categorias atuais são:

- `same_ownership` — transferências de mesma titularidade;
- `investments` — resgates, rendimentos e aplicações financeiras;
- `gambling` — apostas, jogos, loterias e plataformas de jogo.

### Exemplo de edição

```json
{
  "same_ownership": [
    "mesma titularidade",
    "transferencia entre contas",
    "conta propria",
    "transferencia propria",
    "auto transferencia",
    "transferencia interna",
    "movimentacao interna"
  ],
  "investments": [
    "cdb",
    "rendimento",
    "aplicacao",
    "resgate",
    "fundo",
    "corretora",
    "investimento",
    "tesouro",
    "lci",
    "lca",
    "dividendos"
  ],
  "gambling": [
    "bet",
    "aposta",
    "jogo",
    "loteria",
    "premio",
    "pix bet",
    "cassino",
    "sportsbook",
    "blaze",
    "estrela bet"
  ]
}
```

Você pode adicionar novas palavras sem alterar o código principal da aplicação.

> **Nota:** A correspondência usa fronteira de palavra (`\b`), evitando falsos positivos de palavras curtas (ex.: "bet" não casará em "ELIZABETH").

---

## 🏦 Bancos Suportados

A ferramenta detecta automaticamente 23+ instituições financeiras brasileiras:

### Bancos com parser dedicado (alta precisão)
Nubank, C6 Bank, Banco Inter, Itaú, Bradesco, Santander, Caixa Econômica Federal, Banco do Brasil, PicPay.

### Bancos detectados automaticamente (parser genérico robusto)
Sicoob, Sicredi, Will Bank, Neon, Mercado Pago, BTG Pactual, PagBank, Banco Original, Safra, Banrisul, Banco Pan, Crefisa, BV Financeira, HSBC.

### Adicionar novo banco

Edite `config/bank_markers.json` (ou altere `src/bank_detector.py`):

```json
{
  "meubanco": {
    "display_name": "Meu Banco S.A.",
    "markers": ["meu banco", "meu banco s.a."],
    "priority_group": 3
  }
}
```

Para extratos fora do padrão, o **Gemini AI** é o caminho mais robusto — o prompt é banco-agnóstico e adapta-se automaticamente ao layout do documento.

---

## ☁️ Integração com Gemini AI (Opcional)

Para usar extração via IA, configure a variável de ambiente `GEMINI_API_KEY`:

**Localmente (arquivo `.env`):**
```env
GEMINI_API_KEY=sua_chave_aqui
```

**Streamlit Cloud:**
Adicione a variável `GEMINI_API_KEY` nas configurações do app no Streamlit Cloud.

> ⚠️ **Importante:** O arquivo `.env` já está no `.gitignore` para evitar commit acidental de credenciais.

### Características da integração Gemini

- **Prompt banco-agnóstico**: funciona com qualquer banco brasileiro;
- **Cadeia de fallback**: tenta `gemini-3.6-flash` e, em caso de 404, cai automaticamente para `gemini-3.5-flash-lite`;
- **Retry com backoff**: até 3 tentativas em caso de erro 429 (quota/rate-limit);
- **Validação contra gabarito**: compara somatórios extraídos com totais impressos pelo banco;
- **Rastreabilidade**: lançamentos extraídos via IA recebem selo `[IA]` no PDF e 🤖 no Excel/CSV;
- **Consentimento LGPD**: checkbox explícito antes de enviar PDFs à API.

---

## 📊 Relatório Gerado

O relatório PDF contém:

- Cabeçalho repetido em todas as páginas com nome do titular;
- Rodapé com numeração de página ("Página X de Y");
- Cards com indicadores principais (Total Geral, Média Mensal, Média Meses Completos);
- Resumo consolidado por mês com Dias Cobertos, Qtd. Entradas Válidas e Total Apurado;
- Detalhamento de entradas válidas agrupadas por mês com subtotais;
- Tabela de auditoria completa com todos os valores excluídos e motivo;
- Rastreabilidade de lançamentos confirmados manualmente e extraídos via IA;
- Nota metodológica no rodapé;
- **Crédito de autoria** da desenvolvedora.

---

## 🚀 Publicar no GitHub

Se o projeto ainda não estiver conectado ao repositório remoto:

```bash
git init
git add .
git commit -m "feat: implementa ferramenta de apuração de renda via extratos PDF"
git branch -M main
git remote add origin https://github.com/sunstrix/apuracao-renda-extratos-LANA.git
git push -u origin main
```

Se o repositório remoto já existir:

```bash
git add .
git commit -m "feat: atualiza projeto com correções e melhorias"
git push origin main
```

---

## ☁️ Deploy no Streamlit Cloud

### Passo 1 — Acesse o Streamlit Cloud
Abra: https://share.streamlit.io/

### Passo 2 — Faça login com o GitHub
Use a mesma conta que possui acesso ao repositório.

### Passo 3 — Criar novo app
Clique em **New app**.

### Passo 4 — Configurar o app
Selecione:
- **Repository:** `sunstrix/apuracao-renda-extratos-LANA`
- **Branch:** `main`
- **Main file path:** `app.py`

### Passo 5 — Deploy
Clique em **Deploy**.

O Streamlit Cloud irá automaticamente:
- Clonar o repositório;
- Instalar as dependências do `requirements.txt`;
- Executar o comando `streamlit run app.py`.

---

### ⚠️ Observações Importantes

**Limite de memória no Streamlit Cloud**

O plano gratuito do Streamlit Cloud possui limite baixo de memória. Por isso, o projeto foi desenvolvido para:
- Processar arquivos em paralelo com controle de workers;
- Liberar memória após a extração;
- Gerar o PDF somente quando solicitado;
- Evitar carregar todos os PDFs simultaneamente em memória.

Ainda assim, extratos extremamente grandes podem causar falhas em ambiente gratuito.

**PDFs recomendados**

Use preferencialmente PDFs nativos, gerados diretamente pelo banco. PDFs escaneados podem depender de OCR e apresentar:
- Menor precisão;
- Maior tempo de processamento;
- Maior consumo de memória.

**Dados sensíveis**

Como os extratos podem conter informações financeiras pessoais, evite publicar arquivos reais em repositório público. Se necessário, utilize o projeto apenas localmente ou em ambiente privado.

---

## 🐛 Solução de Problemas

### `ModuleNotFoundError: No module named 'src'`

Verifique se você está executando o comando a partir da raiz do projeto:
```bash
streamlit run app.py
```

E se o ambiente virtual está ativado:
```powershell
.venv\Scripts\Activate.ps1
```

Depois reinstale as dependências:
```bash
pip install -r requirements.txt
```

### PDF protegido por senha

A aplicação não processa PDFs protegidos por senha. Remova a proteção antes de enviar o arquivo.

### PDF sem camada de texto

Se o PDF for uma imagem escaneada, o OCR poderá ser usado, desde que o Tesseract esteja instalado corretamente.

### Tesseract não encontrado

Verifique se o Tesseract está instalado e se o executável está no PATH do Windows:
```bash
tesseract --version
```

### Erro de coluna no resumo mensal

Se aparecer `ValueError: Length mismatch` no resumo consolidado, certifique-se de que todos os arquivos (`income_calculator.py`, `app.py`, `report_generator.py`) estão atualizados com a coluna `dias_cobertos`.

### Falsos positivos na exclusão de renda

Se transações legítimas estão sendo excluídas indevidamente (ex.: "ELIZABETH" casando com "bet"), verifique se o `rules_engine.py` está usando a versão com fronteira de palavra (`\b`). A correspondência por substring pura foi substituída por regex com fronteira.

### Erros de arredondamento de centavos

Se os totais não batem com os do extrato, verifique se o `transaction_parser.py` está usando `decimal.Decimal` em vez de `float`. A migração para Decimal elimina erros IEEE 754.

---

## 📌 Status do Projeto

Projeto em produção, contendo:

- Extração de PDF com fallback em camadas (PyMuPDF → pdfplumber → OCR);
- Parsing de transações por banco (23+ instituições);
- Motor de regras de exclusão configurável (com fronteira de palavra);
- **Deduplicação automática** de transações em extratos sobrepostos;
- **Precisão monetária com Decimal** (centavos exatos);
- Revisão manual obrigatória antes da exportação;
- Consolidação mensal com subtotais;
- Relatório executivo em PDF com paginação e cabeçalho/rodapé;
- Exportação em Excel e CSV;
- Integração opcional com Gemini AI (banco-agnóstica);
- **Interface profissional** com tema customizado, hero section e tabs;
- **Crédito de autoria** em todos os artefatos.

Pronto para evolução com novos layouts de extratos, melhorias de parsing e novas regras de negócio.

---

## 👤 Autoria

**Desenvolvido por:** Lana Gleizi Vieira Paes

Crédito visível em:
- Interface Streamlit (rodapé fixo em todas as telas);
- Relatório PDF (após a nota metodológica);
- Aba "Resumo_Mensal" do Excel;
- Linha final do CSV;
- Esta documentação.

---

## 🤝 Contribuidores

- **Alex** — Desenvolvedor BR, manutenção e evolução do projeto
  - GitHub: [@sunstrix](https://github.com/sunstrix)

---

<div align="center">

⭐ Se este projeto foi útil, considere dar uma estrela no GitHub! ⭐

*Feito com ❤️ para automatizar a apuração de renda*

**Desenvolvido por Lana Gleizi Vieira Paes**

</div>