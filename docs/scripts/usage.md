```bash
bash scripts/upload-doc-images-to-r2.sh \
    --remote bot-docs-s3 \
    --bucket bot \
    --prefix docs \
    --rewrite-markdown \
    --public-base-url https://files.astrbot.app
```