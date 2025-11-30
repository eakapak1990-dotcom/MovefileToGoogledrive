import os
import io
import json
import time
import datetime
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

# --- Configuration (ตั้งค่า) ---
SCOPES = ['https://www.googleapis.com/auth/drive'] 
CLIENT_SECRETS_FILE = 'client_secrets.json' 
TOKEN_FILE = 'token.json' 
SYNC_STATE_FILE = 'sync_state.json' 

# ใช้ r'' (Raw String) เพื่อแก้ปัญหา Path Windows
LOCAL_ROOT_FOLDER = r'D:\Design' 
DRIVE_DESTINATION_FOLDER = 'Design_Backup' 
CHUNK_SIZE = 10 * 1024 * 1024 

# --- 1. Authentication & Setup ---
def authenticate_drive():
    """ทำการ Login และขอสิทธิ์การเข้าถึง Google Drive"""
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # กรณี Refresh token หมดอายุ ให้ลบและขอใหม่
                os.remove(TOKEN_FILE)
                return authenticate_drive()
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                CLIENT_SECRETS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        
        with open(TOKEN_FILE, 'w') as token:
            token.write(creds.to_json())
            
    return build('drive', 'v3', credentials=creds)

def get_or_create_folder(service, folder_name, parent_id=None):
    """ค้นหาหรือสร้างโฟลเดอร์บน Drive"""
    query = (f"name='{folder_name}' and mimeType='application/vnd.google-apps.folder' "
             f"and trashed=false")
    if parent_id:
        query += f" and '{parent_id}' in parents"
        
    response = service.files().list(q=query, spaces='drive', fields='files(id, name)').execute()
    files = response.get('files', [])
    
    if files:
        return files[0]['id']
    else:
        file_metadata = {
            'name': folder_name,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        if parent_id:
            file_metadata['parents'] = [parent_id]
            
        folder = service.files().create(body=file_metadata, fields='id').execute()
        print(f"-> Created Drive folder: {folder_name}")
        return folder.get('id')

# --- 2. Helper Functions ---
def load_sync_state():
    """โหลดสถานะการซิงค์"""
    if os.path.exists(SYNC_STATE_FILE):
        try:
            with open(SYNC_STATE_FILE, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {'local_to_drive': {}, 'drive_to_local': {}}
    return {'local_to_drive': {}, 'drive_to_local': {}}

def save_sync_state(state):
    """บันทึกสถานะการซิงค์"""
    with open(SYNC_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=4)

# --- 3. Sync Logic: Local -> Drive ---
def sync_local_file_to_drive(service, filepath, drive_parent_id, state):
    filename = os.path.basename(filepath)
    # ใช้ relpath เพื่อเก็บ path ที่สัมพันธ์กับ root
    try:
        relative_path = os.path.relpath(filepath, LOCAL_ROOT_FOLDER)
    except ValueError:
        # กรณีรันคนละ drive letter
        relative_path = filename

    local_mtime = os.path.getmtime(filepath)
    
    record = state['local_to_drive'].get(relative_path, {})
    drive_file_id = record.get('drive_id')
    last_local_mtime = record.get('local_mtime', 0)
    
    # ถ้าเวลาแก้ไขเท่าเดิม และมี ID อยู่แล้ว ให้ข้าม
    if local_mtime <= last_local_mtime and drive_file_id:
        return # Skip
        
    print(f"\n|-- Syncing Local -> Drive: {filename}...")
    
    media = MediaFileUpload(filepath, resumable=True, chunksize=CHUNK_SIZE)
    file_metadata = {'name': filename, 'parents': [drive_parent_id]}
    
    request = None
    if drive_file_id:
        try:
            # ลอง update ไฟล์เดิม
            request = service.files().update(fileId=drive_file_id, media_body=media)
            print(f"|-- Updating existing file ID: {drive_file_id}")
        except Exception:
            drive_file_id = None
            
    if not drive_file_id:
        # สร้างไฟล์ใหม่
        request = service.files().create(body=file_metadata, media_body=media, fields='id')
        
    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            progress = int(status.progress() * 100)
            print(f"|-- Progress: {progress}% uploaded", end='\r')
            
    if response and response.get('id'):
        new_drive_id = response.get('id')
        state['local_to_drive'][relative_path] = {
            'drive_id': new_drive_id,
            'local_mtime': local_mtime
        }
        print(f"\n|-- Finished. Drive ID: {new_drive_id}")

def sync_local_to_drive(service, state, drive_root_id):
    print(f"\n\n--- [START] Sync: Local ({LOCAL_ROOT_FOLDER}) -> Drive ---")
    
    folder_map = {LOCAL_ROOT_FOLDER: drive_root_id}
    current_local_paths = set()
    
    for root, dirs, files in os.walk(LOCAL_ROOT_FOLDER):
        parent_drive_id = folder_map.get(root)
        if not parent_drive_id: continue

        # เก็บ path สัมพัทธ์ของ folder ปัจจุบัน
        try:
            rel_root = os.path.relpath(root, LOCAL_ROOT_FOLDER)
        except ValueError:
            rel_root = root

        if root != LOCAL_ROOT_FOLDER:
             current_local_paths.add(rel_root)

        for d in dirs:
            local_subpath = os.path.join(root, d)
            f_id = get_or_create_folder(service, d, parent_drive_id)
            folder_map[local_subpath] = f_id
            
        for f in files:
            local_filepath = os.path.join(root, f)
            sync_local_file_to_drive(service, local_filepath, parent_drive_id, state)
            
            try:
                rel_file = os.path.relpath(local_filepath, LOCAL_ROOT_FOLDER)
                current_local_paths.add(rel_file)
            except ValueError:
                pass
            
    # ตรวจสอบการลบ (Deletion Check)
    paths_to_remove = []
    for relative_path, record in state['local_to_drive'].items():
        # สร้าง full path เพื่อเช็คว่ายังมีไฟล์อยู่จริงไหม
        full_path = os.path.join(LOCAL_ROOT_FOLDER, relative_path)
        
        if not os.path.exists(full_path) and record.get('drive_id'):
            print(f"\n-> Deleting on Drive (Local deletion): {relative_path}")
            try:
                service.files().delete(fileId=record['drive_id']).execute()
                paths_to_remove.append(relative_path)
            except Exception as e:
                print(f"Could not delete {relative_path}: {e}")
                # ถ้าหาไม่เจอ แสดงว่าอาจถูกลบไปแล้ว ก็ให้ลบออกจาก state ได้เลย
                if '404' in str(e):
                    paths_to_remove.append(relative_path)

    for path in paths_to_remove:
        del state['local_to_drive'][path]
        
    print("--- [END] Sync: Local -> Drive ---")

# --- 4. Sync Logic: Drive -> Local ---
def sync_drive_to_local(service, state, drive_root_id):
    print(f"\n\n--- [START] Sync: Drive -> Local ({LOCAL_ROOT_FOLDER}) ---")
    
    query = f"'{drive_root_id}' in parents and trashed=false"
    results = service.files().list(q=query, fields='files(id, name, mimeType, modifiedTime)').execute()
    drive_files = results.get('files', [])

    for d_file in drive_files:
        file_id = d_file['id']
        name = d_file['name']
        mime = d_file['mimeType']
        local_path = os.path.join(LOCAL_ROOT_FOLDER, name)
        
        # แปลงเวลา
        dt_obj = datetime.datetime.strptime(d_file['modifiedTime'], "%Y-%m-%dT%H:%M:%S.%fZ")
        drive_mtime = dt_obj.timestamp()
        
        record = state['drive_to_local'].get(file_id, {})
        last_drive_mtime = record.get('drive_mtime', 0)
        
        if drive_mtime > last_drive_mtime:
            if mime == 'application/vnd.google-apps.folder':
                if not os.path.exists(local_path):
                    os.makedirs(local_path)
                    print(f"-> Created Local Folder: {name}")
            else:
                print(f"\n|-- Downloading: {name}...")
                request = service.files().get_media(fileId=file_id)
                fh = io.BytesIO()
                downloader = MediaIoBaseDownload(fh, request)
                done = False
                while done is False:
                    status, done = downloader.next_chunk()
                    print(f"|-- Download Progress: {int(status.progress()*100)}%", end='\r')
                
                with open(local_path, 'wb') as f:
                    f.write(fh.getvalue())
                
                # Update local time to match drive time
                os.utime(local_path, (drive_mtime, drive_mtime))
                print(f"\n|-- Downloaded: {local_path}")
            
            state['drive_to_local'][file_id] = {
                'drive_mtime': drive_mtime,
                'local_path': local_path
            }

    print("--- [END] Sync: Drive -> Local ---")

# --- Main Execution ---
def main():
    if not os.path.isdir(LOCAL_ROOT_FOLDER):
        print(f"Error: Folder path not found: {LOCAL_ROOT_FOLDER}")
        return

    print("Authenticating...")
    service = authenticate_drive()
    
    print("Checking Drive Destination...")
    drive_root_id = get_or_create_folder(service, DRIVE_DESTINATION_FOLDER)
    
    sync_state = load_sync_state()

    # 1. Drive -> Local (Pull changes)
    sync_drive_to_local(service, sync_state, drive_root_id)

    # 2. Local -> Drive (Push changes & Deletions)
    sync_local_to_drive(service, sync_state, drive_root_id)

    save_sync_state(sync_state)
    print("\n\n=== Synchronization Complete ===")

if __name__ == '__main__':
    main()