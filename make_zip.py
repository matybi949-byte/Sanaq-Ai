import os
import zipfile

def create_archive():
    filename = "sanaq_final.zip"
    # Все необходимые исходные файлы проекта
    files_to_pack = [
        'admin_router.py', 'ai_service.py', 'analytics.py', 'config.py', 
        'dashboard.py', 'database.py', 'db_backup.py', 'error_notifier.py',
        'heartbeat.py', 'kaspi_payment.py', 'main.py', 'messengers_router.py',
        'models.py', 'omnichannel.py', 'openai_service.py', 'orders.py', 
        'payments.py', 'rate_limiter.py', 'requirements.txt', 'safety.py', 
        'tg_admin_bot.py', 'webhook_router.py', '.env.example', 'Dockerfile',
        'docker-compose.yml', 'README.md',
        'test_bot.py', 'test_db_ai_manager.py', 'test_fault_tolerance_validation.py',
        'test_messengers_router.py', 'test_multimodal.py', 'test_new_modules.py',
        'test_order_checkout.py', 'test_payments.py'
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