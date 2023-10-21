from fastapi import FastAPI, Depends, HTTPException, Path
from typing import List, Dict, Optional, Any
import argparse
from pydantic import BaseModel
from ap_package import append_ap_packages_to_sys_path
append_ap_packages_to_sys_path()
import json
import OpenSSL
import datetime


from config import Configuration, get_session
from rt_m1_client.types import ResourceId, ApplicationId, ContentHostingConfiguration, ConsumptionReportingConfiguration
from rt_m1_client.exceptions import M1Error


app = FastAPI()
config = Configuration()

class DeleteSessionArgs(BaseModel):
    provisioning_session: Optional[str]
    ingesturl: str
    entrypoint: str

def get_config():
    return Configuration()

async def __prettyPrintCertificate(cert: str, indent: int = 0) -> None:
    cert_desc = {}
    try:
        x509 = OpenSSL.crypto.load_certificate(OpenSSL.crypto.FILETYPE_PEM, cert)
    except OpenSSL.crypto.Error as err:
        print(f'{" "*indent} Certificate not understood as PEM data: {err}')
        return
    serial = x509.get_serial_number()
    subject = x509.get_subject()
    issuer = x509.get_issuer()
    start_str = x509.get_notBefore()
    if isinstance(start_str, bytes):
        start_str = start_str.decode('utf-8')
    start = datetime.datetime.strptime(start_str, '%Y%m%d%H%M%SZ').replace(tzinfo=datetime.timezone.utc)
    end_str = x509.get_notAfter()
    if isinstance(end_str, bytes):
        end_str = end_str.decode('utf-8')
    end = datetime.datetime.strptime(end_str, '%Y%m%d%H%M%SZ').replace(tzinfo=datetime.timezone.utc)
    subject_key = None
    issuer_key = None
    sans = []
    for ext_num in range(x509.get_extension_count()):
        ext = x509.get_extension(ext_num)
        ext_name = ext.get_short_name().decode('utf-8')
        if ext_name == "subjectKeyIdentifier":
            subject_key = str(ext)
        elif ext_name == "authorityKeyIdentifier":
            issuer_key = str(ext)
        elif ext_name == "subjectAltName":
            sans += [s.strip() for s in str(ext).split(',')]
    cert_info_prefix=' '*indent
    cert_desc=f'{cert_info_prefix}Serial = {serial}\n{cert_info_prefix}Not before = {start}\n{cert_info_prefix}Not after = {end}\n{cert_info_prefix}Subject = {__formatX509Name(subject)}\n'
    if subject_key is not None:
        cert_desc += f'{cert_info_prefix}          key={subject_key}\n'
    cert_desc += f'{cert_info_prefix}Issuer = {__formatX509Name(issuer)}'
    if issuer_key is not None:
        cert_desc += f'\n{cert_info_prefix}         key={issuer_key}'
    if len(sans) > 0:
        cert_desc += f'\n{cert_info_prefix}Subject Alternative Names:'
        cert_desc += ''.join([f'\n{cert_info_prefix}  {san}' for san in sans])
    return cert_desc

def __formatX509Name(x509name: OpenSSL.crypto.X509Name) -> str:
    ret = ",".join([f"{name.decode('utf-8')}={value.decode('utf-8')}" for name,value in x509name.get_components()])
    return ret

# Creates new provisioning session
# new-provisioning-session -e MyAppId -a MyASPId
@app.post("/create_session")
async def new_provisioning_session():

    session = await get_session(config)
    args = argparse.Namespace(app_id="MyAppId", asp_id="MyASPId")
    app_id = args.app_id or config.get('external_app_id')
    asp_id = args.asp_id or config.get('asp_id')

    provisioning_session_id: Optional[ResourceId] = await session.createDownlinkPullProvisioningSession(
        ApplicationId(app_id),
        ApplicationId(asp_id) if asp_id else None)
    
    if provisioning_session_id is None:
        return {"Failed to create a new provisioning session"}
        
    return {provisioning_session_id}


# Remove particular provisioning session
# del-stream -p ${provisioning_session_id}
@app.delete("/delete_session/{provisioning_session_id}")
async def cmd_delete_stream(provisioning_session_id: str, config: Configuration = Depends(get_config)) -> int:    
    session = await get_session(config)
    
    result = await session.provisioningSessionDestroy(provisioning_session_id)
    if result is None:
        print(f'Provisioning Session {provisioning_session_id} not found')
        return 1
    
    if not result:
        print(f'Failed to destroy Provisioning Session {provisioning_session_id}')
        return 1
    
    print(f'Provisioning Session {provisioning_session_id} and all its resources were destroyed')
    return 0

# Create CHC from json
# set-stream -p ${provisioning_session_id} ~/rt-5gms-application-function/examples/ContentHostingConfiguration_Big-Buck-Bunny_pull-ingest.json

@app.post("/set_stream/{provisioning_session_id}")
async def set_stream(provisioning_session_id: str, config: Configuration = Depends(get_config)) -> int:
    session = await get_session(config)
    
    json_path = "/home/stepski/rt-5gms-application-function/examples/ContentHostingConfiguration_Llama-Drama_pull-ingest.json"
    with open(json_path, 'r') as f:
        chc = json.load(f)
    
    old_chc = await session.contentHostingConfigurationGet(provisioning_session_id)
    
    if old_chc is None:
        result = await session.contentHostingConfigurationCreate(provisioning_session_id, chc)
    else:
        for dc in chc['distributionConfigurations']:
            for strip_field in ['canonicalDomainName', 'baseURL']:
                if strip_field in dc:
                    del dc[strip_field]
        result = await session.contentHostingConfigurationUpdate(provisioning_session_id, chc)

    if not result:
        print(f'Failed to set hosting for provisioning session {provisioning_session_id}')
        return 1
    
    print(f'Hosting set for provisioning session {provisioning_session_id}')
    return 0


# Retrieve Session Details
# list -v
@app.get("/details", response_model=List[Dict[str, Any]])
async def list_verbose(config: Configuration = Depends(get_config)) -> List[Dict[str, Any]]:
    session = await get_session(config)
    ps_ids = await session.provisioningSessionIds()
    results = []

    for ps_id in ps_ids:
        ps_dict = {"id": ps_id, "Certificates": [], "ContentHostingConfiguration": None, "ConsumptionReportingConfiguration": None}
        
        certs = await session.certificateIds(ps_id)
        for cert_id in certs:
            cert = await session.certificateGet(ps_id, cert_id)
            if cert:
                ps_dict["Certificates"].append(await __prettyPrintCertificate(cert))
        
        chc = await session.contentHostingConfigurationGet(ps_id)
        if chc:
            ps_dict["ContentHostingConfiguration"] = chc
            
        crc = await session.consumptionReportingConfigurationGet(ps_id)
        if crc:
            ps_dict["ConsumptionReportingConfiguration"] = crc
        
        results.append(ps_dict)

    return results