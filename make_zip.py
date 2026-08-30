import os
import zipfile

def create_archive():
    filename = "sanaq_final.zip"
    # Только нужные файлы проекта
    files_to_pack = [
        'admin_router.py', 'ai_service.py', 'analytics.py', 'config.py', 
        'database.py', 'main.py', 'models.py', 'omnichannel.py', 
        'openai_service.py', 'orders.py', 'payments.py', 'rate_limiter.py', 
        'requirements.txt', 'safety.py', 'test_bot.py', 'test_db_ai_manager.py', 
        'test_multimodal.py', 'test_order_checkout.py', 'test_payments.py', 
        'tg_admin_bot.py', 'webhook_router.py', '.env.example'
    ]
    
    with zipfile.ZipFile(filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for f in files_to_pack:
            if os.path.exists(f):
                zipf.write(f)
                print(f"Добавлен: {f}")
            else:
                print(f"Пропущен (нет файла): {f}")
                
    print(f"\nГОТОВО! Файл архива создан: {filename}")

if __name__ == "__main__":
    create_archive()