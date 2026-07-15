import logging
import os
from datetime import datetime, timezone

import azure.functions as func
import requests
from azure.identity import DefaultAzureCredential
from azure.storage.blob import ContainerClient

app = func.FunctionApp()

def download_and_persist(url:str, file_path:str, file_prefix:str):
    logging.info("Start downloading Transport Victoria data ...")

    headers = {"KeyId": os.environ["TRANSPORT_VIC_API_KEY"]}

    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()

    # feed = gtfs_realtime_pb2.FeedMessage()
    # feed.ParseFromString(resp.content)  # binary protobuf → Python objects
    # print("Feed timestamp:", feed.header.timestamp)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    conn = os.environ["GTFS_CONTAINER_SAS_URL"]
    container = ContainerClient.from_container_url(
        conn, credential=DefaultAzureCredential()
    )

    # 4. Write the blob with a timestamped name.
    # utcnow = datetime.datetime.now(datetime.timezone.utc)
    # blob_name = f"{utcnow:%Y/%m/%d}/trip-updates-{utcnow:%H%M%S}.pb"
    blob_name = f"{file_path}/date={ts[:8]}/{file_prefix}_{ts}.pb"
    container.upload_blob(name=blob_name, data=resp.content, overwrite=True)

    logging.info("Wrote blob: %s", blob_name)


@app.timer_trigger(
    schedule="0 */5 * * * *",
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=False,
)
def GTFSRealtimeTripUpdatesDownload(myTimer: func.TimerRequest) -> None:

    if myTimer.past_due:
        logging.info("The timer is past due!")

    logging.info("Start downloading Transport Victoria data ...")
    url = "https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/vline/trip-updates"
    file_path = "landing/vline_trip_updates"

    download_and_persist(url=url, file_path=file_path, file_prefix="vline_tu")
    # for entity in feed.entity:
    #     if entity.HasField("trip_update"):
    #         tu = entity.trip_update
    #         print(tu.trip.trip_id, tu.trip.start_date)
    #         for stu in tu.stop_time_update:
    #             delay = stu.arrival.delay if stu.HasField("arrival") else None
    #             print("  stop:", stu.stop_id, "arrival delay (s):", delay)


@app.timer_trigger(
    schedule="0 */2 * * * *",
    arg_name="myTimer",
    run_on_startup=False,
    use_monitor=False,
)
def GTFSRealtimeVehiclePositionsDownload(myTimer: func.TimerRequest) -> None:

    if myTimer.past_due:
        logging.info("The timer is past due!")

    logging.info("Start downloading Transport Victoria data ...")
    url = "https://api.opendata.transport.vic.gov.au/opendata/public-transport/gtfs/realtime/v1/vline/vehicle-positions"
    file_path = "landing/vline_vehicle_positions"
    download_and_persist(url=url, file_path=file_path, file_prefix="vline_vp")

