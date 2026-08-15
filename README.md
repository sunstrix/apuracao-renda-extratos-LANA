\# 📊 Apuração de Renda via Extratos PDF



Ferramenta desenvolvida em \*\*Python + Streamlit\*\* para consolidar múltiplos extratos bancários em PDF, aplicar regras de negócio de inclusão/exclusão de renda e gerar automaticamente um \*\*relatório executivo em PDF\*\*.



O projeto foi estruturado para receber vários extratos de bancos e períodos diferentes, extrair as transações, identificar entradas relevantes, excluir movimentações que não representam renda efetiva e apresentar uma análise consolidada.



\---



\## 🎯 Objetivo



A ferramenta permite que o usuário:



\- anexe múltiplos extratos bancários em PDF;

\- processe lançamentos financeiros automaticamente;

\- consolide entradas por mês;

\- exclua movimentações que não devem entrar na apuração de renda;

\- visualize uma prévia dos resultados na interface;

\- gere um relatório executivo em PDF para download.



\---



\## 🧩 Funcionalidades



\- Upload múltiplo de arquivos PDF;

\- extração de texto de PDFs nativos;

\- fallback com PyMuPDF quando necessário;

\- fallback com OCR para PDFs escaneados;

\- parsing de datas e valores monetários no padrão brasileiro;

\- motor de regras configurável para exclusão de renda;

\- consolidação mensal das entradas válidas;

\- cálculo de indicadores:

&#x20; - Total Geral Apurado;

&#x20; - Média Mensal Geral;

&#x20; - Média de Meses Completos;

\- tabela detalhada de entradas válidas;

\- tabela de auditoria com entradas excluídas e motivo;

\- geração de relatório executivo em PDF;

\- interface simples e direta via Streamlit.



\---



\## 🛠️ Stack Técnica



\- \*\*Python 3.10+\*\*

\- \*\*Streamlit\*\* — interface web

\- \*\*pdfplumber\*\* — extração principal de texto e tabelas

\- \*\*PyMuPDF\*\* — fallback de extração

\- \*\*pandas\*\* — organização e visualização tabular

\- \*\*reportlab\*\* — geração do relatório PDF

\- \*\*python-dateutil\*\* — normalização de datas

\- \*\*pytesseract\*\* — OCR para PDFs escaneados

\- \*\*Pillow\*\* — suporte a imagens no OCR

\- \*\*pytest\*\* — testes unitários



\---



\## 📂 Estrutura do Projeto



```text

apuracao-renda-extratos-LANA/

├── app.py                      # Aplicação principal Streamlit

├── requirements.txt            # Dependências do projeto

├── README.md                   # Documentação do projeto

├── .gitignore                  # Arquivos ignorados pelo Git

├── config/

│   └── exclusion\_keywords.json # Palavras-chave para exclusão de renda

├── src/

│   ├── \_\_init\_\_.py

│   ├── pdf\_extractor.py        # Extração de texto dos PDFs

│   ├── transaction\_parser.py   # Interpretação das transações

│   ├── rules\_engine.py         # Regras de inclusão/exclusão de renda

│   ├── income\_calculator.py    # Cálculos e consolidações

│   └── report\_generator.py     # Geração do relatório PDF

└── tests/

&#x20;   └── test\_rules\_engine.py    # Testes unitários das regras

```



\---



\## ▶️ Como Rodar Localmente



\### 1. Clonar o repositório



```bash

git clone https://github.com/sunstrix/apuracao-renda-extratos-LANA.git

cd apuracao-renda-extratos-LANA

```



\### 2. Criar ambiente virtual



No Windows:



```powershell

python -m venv .venv

```



\### 3. Ativar o ambiente virtual



No PowerShell:



```powershell

.\\.venv\\Scripts\\Activate.ps1

```



Se houver bloqueio de execução de scripts, use:



```powershell

Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\\.venv\\Scripts\\Activate.ps1

```



\### 4. Instalar as dependências



```powershell

pip install -r requirements.txt

```



\### 5. Executar a aplicação



```powershell

streamlit run app.py

```



A aplicação abrirá automaticamente no navegador, normalmente em:



```text

http://localhost:8501

```



\---



\## 🔎 OCR para PDFs Escaneados



A aplicação tenta primeiro extrair a camada de texto do PDF. Se o PDF for uma imagem escaneada, ela pode usar OCR como fallback.



Para OCR no Windows, é necessário instalar o Tesseract OCR.



Exemplo usando Winget:



```powershell

winget install UB-Mannheim.TesseractOCR

```



Após instalar, verifique se o executável `tesseract` está acessível no PATH do Windows.



> Observação importante: PDFs nativos com camada textual são muito mais rápidos e confiáveis do que PDFs escaneados.



\---



\## 🧪 Testes



Para executar os testes unitários:



```powershell

pytest tests/

```



\---



\## ⚙️ Configuração das Regras de Exclusão



As palavras-chave usadas para excluir lançamentos da apuração de renda ficam no arquivo:



```text

config/exclusion\_keywords.json

```



As categorias atuais são:



\- `same\_ownership` — transferências de mesma titularidade;

\- `investments` — resgates, rendimentos e aplicações financeiras;

\- `gambling` — apostas, jogos, loterias e plataformas de jogo.



Exemplo de edição:



```json

{

&#x20; "same\_ownership": \[

&#x20;   "mesma titularidade",

&#x20;   "conta propria"

&#x20; ],

&#x20; "investments": \[

&#x20;   "rendimento",

&#x20;   "cdb",

&#x20;   "resgate"

&#x20; ],

&#x20; "gambling": \[

&#x20;   "bet",

&#x20;   "aposta",

&#x20;   "cassino"

&#x20; ]

}

```



Você pode adicionar novas palavras sem alterar o código principal da aplicação.



\---



\## 📄 Relatório Gerado



O relatório PDF contém:



\- título do relatório;

\- nome do titular;

\- instituições financeiras identificadas;

\- data de geração;

\- cards com indicadores principais;

\- resumo consolidado por mês;

\- detalhamento das entradas válidas;

\- tabela de auditoria com valores excluídos;

\- nota metodológica no rodapé.



\---



\## 🚀 Publicar no GitHub



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

git commit -m "feat: implementa ferramenta de apuração de renda via extratos PDF"

git push origin main

```



\---



\## ☁️ Deploy no Streamlit Cloud



\### Passo 1 — Acesse o Streamlit Cloud



Abra:



```text

https://share.streamlit.io/

```



\### Passo 2 — Faça login com o GitHub



Use a mesma conta que possui acesso ao repositório:



```text

https://github.com/sunstrix/apuracao-renda-extratos-LANA

```



\### Passo 3 — Criar novo app



Clique em:



```text

New app

```



\### Passo 4 — Configurar o app



Selecione:



\- Repository: `sunstrix/apuracao-renda-extratos-LANA`

\- Branch: `main`

\- Main file path: `app.py`



\### Passo 5 — Deploy



Clique em:



```text

Deploy

```



O Streamlit Cloud irá automaticamente:



1\. clonar o repositório;

2\. instalar as dependências do `requirements.txt`;

3\. executar o comando `streamlit run app.py`.



\---



\## ⚠️ Observações Importantes



\### Limite de memória no Streamlit Cloud



O plano gratuito do Streamlit Cloud possui limite baixo de memória.



Por isso, o projeto foi desenvolvido para:



\- processar um arquivo por vez;

\- liberar memória após a extração;

\- gerar o PDF somente quando solicitado;

\- evitar carregar todos os PDFs simultaneamente em memória.



Ainda assim, extratos extremamente grandes podem causar falhas em ambiente gratuito.



\### PDFs recomendados



Use preferencialmente PDFs nativos, gerados diretamente pelo banco.



PDFs escaneados podem depender de OCR e apresentar:



\- menor precisão;

\- maior tempo de processamento;

\- maior consumo de memória.



\### Dados sensíveis



Como os extratos podem conter informações financeiras pessoais, evite publicar arquivos reais em repositório público.



Se necessário, utilize o projeto apenas localmente ou em ambiente privado.



\---



\## 🧯 Solução de Problemas



\### `ModuleNotFoundError: No module named 'src'`



Verifique se você está executando o comando a partir da raiz do projeto:



```powershell

streamlit run app.py

```



E se o ambiente virtual está ativado:



```powershell

.\\.venv\\Scripts\\Activate.ps1

```



Depois reinstale as dependências:



```powershell

pip install -r requirements.txt

```



\### PDF protegido por senha



A aplicação não processa PDFs protegidos por senha. Remova a proteção antes de enviar o arquivo.



\### PDF sem camada de texto



Se o PDF for uma imagem escaneada, o OCR poderá ser usado, desde que o Tesseract esteja instalado corretamente.



\### Tesseract não encontrado



Verifique se o Tesseract está instalado e se o executável está no PATH do Windows.



\---



\## 🖼️ Exemplo da Aplicação



<!-- Substitua este comentário por uma imagem real após o primeiro teste.

Exemplo:



!\[Apuração de Renda](assets/screenshot.png)



\-->



\---



\## 📌 Status do Projeto



Projeto inicial funcional, contendo:



\- extração de PDF;

\- parsing de transações;

\- regras de exclusão;

\- consolidação mensal;

\- relatório executivo em PDF.



Pronto para evolução com novos layouts de extratos, melhorias de parsing e novas regras de negócio.

