# Copia este archivo como "local_settings.py" (en la misma carpeta que app.py) y llena tus datos.
# local_settings.py NO se sube a GitHub (está en .gitignore): aquí van tus secretos.

WEBHOOK_URL = "https://hooks.zapier.com/hooks/catch/29155576/4183jjx/"   # la URL de "Catch Hook" de Zapier, por ejemplo "https://hooks.zapier.com/hooks/catch/..."
EDITOR_EMAIL = ""  # correo que recibe el aviso de documentos largos
PAGE_LIMIT = 10    # más de estas páginas = documento largo
REAL_EMAILS_ENABLED = 1  # 0 = nunca enviar correos (solo se simulan en la consola)
EMAIL_DAILY_LIMIT = 20   # máximo de correos reales por día (protege tu cuota de Zapier)
