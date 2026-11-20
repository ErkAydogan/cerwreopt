# RabbitMQ bağlantısı: kalıcı (durable) "reopt_jobs" kuyruğu, kalıcı (persistent) mesajlar.
# Yayıncı onayı (publisher confirm) açık: broker mesajı aldığını onaylamadan API 202 dönmez.
import json

import pika

QUEUE = "reopt_jobs"


def connect(url: str) -> pika.BlockingConnection:
    return pika.BlockingConnection(pika.URLParameters(url))


def declare(channel) -> None:
    # durable=True: broker yeniden başlasa da kuyruk tanımı kaybolmaz
    channel.queue_declare(queue=QUEUE, durable=True)


class Publisher:
    def __init__(self, url: str):
        self.url = url

    def publish(self, job_id: str) -> None:
        # Demo için istek başına bağlantı: basit ve açıklanabilir. Canlıda kalıcı bağlantı/kanal havuzu.
        with connect(self.url) as conn:
            channel = conn.channel()
            declare(channel)
            channel.confirm_delivery()  # basic_publish, broker onaylamazsa hata fırlatır
            channel.basic_publish(
                exchange="",
                routing_key=QUEUE,
                body=json.dumps({"job_id": job_id}).encode(),
                properties=pika.BasicProperties(
                    delivery_mode=pika.DeliveryMode.Persistent,  # mesaj diske yazılır
                    content_type="application/json",
                ),
                mandatory=True,  # kuyruğa yönlenemezse sessizce kaybolmasın
            )

    def ping(self) -> bool:
        with connect(self.url):
            return True
