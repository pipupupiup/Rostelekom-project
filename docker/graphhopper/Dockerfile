# GraphHopper (https://github.com/graphhopper/graphhopper) — линии маршрутов по дорогам для карты ADO.
# Официального образа нет; собираем из jar релиза с Maven Central. Нужен доступ к интернету при сборке.
FROM eclipse-temurin:21-jre
ARG GH_VERSION=11.0
RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*
ADD https://repo1.maven.org/maven2/com/graphhopper/graphhopper-web/${GH_VERSION}/graphhopper-web-${GH_VERSION}.jar /gh/graphhopper-web.jar
COPY config.yml /gh/config.yml
COPY entrypoint.sh /gh/entrypoint.sh
RUN chmod +x /gh/entrypoint.sh
EXPOSE 8989
ENTRYPOINT ["/gh/entrypoint.sh"]
