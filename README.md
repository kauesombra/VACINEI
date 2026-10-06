# Vacinei

Sistema web que digitaliza carteiras de vacinação (foto ou PDF) por OCR e
consolida tudo em um histórico vacinal único por usuário.
Projeto de TCC — Unifor, Ciência da Computação.

## Como funciona
1. **Upload** de imagem (JPG/PNG/WebP…) ou PDF da carteira.
2. **Pré-processamento** (OpenCV): correção de inclinação, remoção de sombra/iluminação
   irregular, binarização e nitidez. Várias variantes são testadas e vence a que
   gera mais registros válidos.
3. **OCR** (Tesseract, idioma `por`) + **parser** que extrai vacina, dose e data
   (catálogo de vacinas e apelidos em `vaccines.py`).
4. **Revisão**: o usuário confere/corrige o que foi lido antes de salvar
   (OCR erra, principalmente em letra manuscrita).
5. **Consolidação**: registros iguais vindos de documentos diferentes são
   unificados, sem duplicar; dose faltante é completada por outro documento.
6. **Histórico**: visão por vacina ou cronológica, filtro, inclusão manual,
   exportação CSV e impressão.

## Exportação FHIR
No histórico, o botão **Exportar FHIR** baixa um `Bundle` HL7 FHIR R4 com um `Patient`
(só o nome) e um `Immunization` por dose (`fhir_export.py`).

- Vacinas são codificadas em CVX (código internacional do CDC). O Brasil (RNDS) usa terminologia
  própria de imunobiológicos; para integrar de verdade, troque o mapeamento seguindo o guia adotado.
- Conferido contra os modelos FHIR R4B da biblioteca `fhir.resources` (nomes e tipos dos campos).
  Isso **não** é validação completa de perfil: para isso, use o `$validate` do servidor público
  do HAPI FHIR (https://hapi.fhir.org), apenas com dados fictícios.

## Rodar
Requisitos do sistema: Python 3.10+, `tesseract-ocr` e `poppler-utils`
(Debian/Ubuntu: `sudo apt install tesseract-ocr poppler-utils`).

    pip install -r requirements.txt
    python app.py            # http://127.0.0.1:5000

O pacote de português do Tesseract já está em `tessdata/`.

Variáveis opcionais: `TESSERACT_CMD`, `POPPLER_PATH`, `PORT`, `VACINEI_DATA` (pasta do banco e dos uploads),
`VACINEI_SECRET`, `VACINEI_HTTPS=1` (cookies seguros em produção).
Em produção use um servidor WSGI (ex.: gunicorn) atrás de HTTPS.

## Testes
    python -m unittest discover -s tests          # 23 testes
    python tests/eval_ocr.py                      # acerto do OCR nas amostras

Resultado nas amostras sintéticas: 12/12 na imagem limpa e no PDF;
11/12 na foto inclinada com sombra (uma dose não reconhecida,
corrigível na tela de revisão).

## Privacidade (LGPD)
Dados de vacinação são dados pessoais de saúde. O cadastro exige consentimento,
os arquivos ficam em `instance/uploads` e a página "Privacidade e seus dados"
explica uso, exportação e exclusão. Não substitui a carteira oficial nem o Meu SUS Digital.

## Limitações
O Tesseract lê texto impresso, mas **não lê bem letra de mão**. Para cadernetas manuscritas,
o usuário confere e digita as doses na tela de revisão ("Adicionar linha") ou no histórico.
Todo o processamento é local: nenhuma imagem é enviada a serviços externos e nada é pago.
