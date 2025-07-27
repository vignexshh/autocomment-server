from fastapi import FastAPI, Request
from pydantic import BaseModel
from datetime import datetime, timedelta
from google.cloud import secretmanager
from google.oauth2 import service_account
import json
import requests
import os
from dotenv import load_dotenv

from motor.motor_asyncio import AsyncIOMotorClient
import pymongo
import aiohttp


# Add MongoDB connection
MONGODB_URL = os.getenv("MONGODB_URL")
DATABASE_NAME = os.getenv("DATABASE_NAME", "autocomment")
COLLECTION_NAME = "channel_connections"

# Initialize MongoDB client
mongo_client = AsyncIOMotorClient(MONGODB_URL)
database = mongo_client[DATABASE_NAME]
collection = database[COLLECTION_NAME]


load_dotenv('creds/.env.local')

app  = FastAPI()


credentials_path =r'C:\Users\vigne\codes\autocomment-backend\creds\arctic-depth-466609-i0-be97ce7e244d.json'
credentials = service_account.Credentials.from_service_account_file(credentials_path)
async_client = secretmanager.SecretManagerServiceClient(credentials=credentials)


class TokenData(BaseModel):
    access_token: str
    refresh_token: str
    expires_in:int
    channel_id:str

@app.post("/webhook/youtube-auth")
async def handle_webhook(data: TokenData):
    try:

        expiration_time = datetime.now().timestamp() + data.expires_in

        token_storage = {
            "access_token": data.access_token,
            "refresh_token": data.refresh_token,
            "expires_at": expiration_time,
            "channel_id": data.channel_id,  
            "recieved_at": datetime.now().isoformat() 
        }
        project_id = os.getenv("GCP_PROJECT_ID")  # Replace with your actual project ID
        if not project_id:
            print("Error: GCP_PROJECT_ID environment variable is not set.")
            return {
            "status": "error",
            "message": "GCP_PROJECT_ID environment variable is not set."
            }
        secret_id = data.channel_id

        
        await store_token_in_secret_manager(
            project_id, secret_id, data.access_token, 
            data.refresh_token, data.expires_in
        )

        return {
            "status": "success",
            "message": "Tokens recieved and processed",
            "expires_at": expiration_time
        }
    except Exception as e:
        print(f"Error processing webhook: {str(e)}")
        return {
            "status":"error",
            "message": "Failed to process tokens"
        }
    
async def store_token_in_secret_manager(project_id, secret_id, access_token, refresh_token, expires_in_seconds):
    client = secretmanager.SecretManagerServiceAsyncClient(credentials=credentials)
    expiration_time_dt = datetime.now() + timedelta(seconds=expires_in_seconds)
    
    token_storage = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expiration_time_dt.timestamp(),
        "received_at": datetime.now().isoformat()
    }

    json_payload = json.dumps(token_storage)
    
    # Add the Secret Manager logic here
    parent = f"projects/{project_id}"
    
    try:
        # Try to add a new version to existing secret
        secret_name = f"{parent}/secrets/{secret_id}"
        response = await client.add_secret_version(
            request={
                "parent": secret_name,
                "payload": {"data": json_payload.encode("UTF-8")},
            }
        )
        print(f"Added secret version: {response.name}")
        
    except Exception as secret_error:
        # If secret doesn't exist, create it first
        if "not found" in str(secret_error).lower():
            secret = await client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_id,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
            
            response = await client.add_secret_version(
                request={
                    "parent": secret.name,
                    "payload": {"data": json_payload.encode("UTF-8")},
                }
            )
            print(f"Created secret and added version: {response.name}")

            try:
                # Get channel details first
                channel_details = await get_youtube_channel_details_public(secret_id)
                
                connection_document = {
                    "channel_id": secret_id,
                    "connectionCreatedOn": datetime.now(),
                    "channel_name": channel_details.get("channel_name") if channel_details else None,
                    "profile_image_url": channel_details.get("profile_image_url") if channel_details else None,
                    "subscriber_count": channel_details.get("subscriber_count") if channel_details else None,
                    "description": channel_details.get("description") if channel_details else None
                }

                result = await collection.insert_one(connection_document)
                print(f"Added channel connection to MongoDB: {result.inserted_id}")
            
            except Exception as mongo_error:
                print(f"Error adding to MongoDB: {str(mongo_error)}")
        else:
            raise secret_error
        

async def get_token_from_secret_manager(project_id, secret_id, version_id="latest"):
    client = secretmanager.SecretManagerServiceAsyncClient(credentials=credentials)
    name = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"

    try:
        response = await client.access_secret_version(request={'name' : name})
        secret_data = response.payload.data.decode("UTF-8")
        token_data  = json.loads(secret_data)

        return token_data
    
    except Exception as e:
        print(f"Error retrieving secret: {str(e)}")
        raise e
    

async def refresh_youtube_token(refresh_token):
    """
    Refresh YouTube access token using refresh token
    """
    refresh_url = "https://oauth2.googleapis.com/token"
    
    # You'll need to add your OAuth2 client credentials
    client_id = os.getenv("CLIENT_ID")  # Replace with your actual client ID
    client_secret = os.getenv("CLIENT_SECRET")  # Replace with your actual client secret

    if not client_id or not client_secret:
        print("Error: CLIENT_ID or CLIENT_SECRET environment variable is not set.")
        raise Exception("CLIENT_ID or CLIENT_SECRET environment variable is not set.")
    
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret
    }
    
    try:
        response = requests.post(refresh_url, data=payload)
        response.raise_for_status()
        
        token_data = response.json()
        return {
            "access_token": token_data["access_token"],
            "expires_in": token_data.get("expires_in", 3600),
            "refresh_token": refresh_token  # Keep the same refresh token
        }
        
    except Exception as e:
        print(f"Error refreshing token: {str(e)}")
        raise e


@app.get("/tokens/youtube")
async def get_youtube_tokens(channel_id:str):
    try:
        project_id = os.getenv("GCP_PROJECT_ID")
        secret_id = channel_id

        token_data = await get_token_from_secret_manager(project_id, secret_id)

        current_time = datetime.now().timestamp()
        expires_at = token_data.get("expires_at", 0)
        is_expired = current_time > expires_at
        # Calculate time until expiration
        time_until_expiry = expires_at - current_time if not is_expired else 0

        if is_expired:
            logging.getLogger("uvicorn.error").info("token expired, generating new token")
            # Token is expired, refresh it
            refresh_token = token_data.get("refresh_token")
            if not refresh_token:
                return {
                    "status": "error",
                    "message": "No refresh token available"
                }
            
            # Refresh the token
            new_token_data = await refresh_youtube_token(refresh_token)
            
            # Store the new token in Secret Manager
            await store_token_in_secret_manager(
                project_id, secret_id,
                new_token_data["access_token"],
                new_token_data["refresh_token"],
                new_token_data["expires_in"]
            )
            
            # Calculate new expiration time
            new_expires_at = datetime.now().timestamp() + new_token_data["expires_in"]
            
            return {
                "status": "success",
                "data": {
                    "channel_id": channel_id,
                    "access_token": new_token_data["access_token"],
                    "refresh_token": new_token_data["refresh_token"],
                    "expires_at": new_expires_at,
                    "received_at": datetime.now().isoformat(),
                    "is_expired": False,
                    "expires_in_seconds": new_token_data["expires_in"],
                    "refreshed": True
                }
            }
        else:
            # Token is still valid
            return {
                "status": "success",
                "data": {
                    "channel_id": channel_id,
                    "access_token": token_data.get("access_token"),
                    "refresh_token": token_data.get("refresh_token"),
                    "expires_at": expires_at,
                    "received_at": token_data.get("received_at"),
                    "is_expired": is_expired,
                    "expires_in_seconds": max(0, int(time_until_expiry)),
                    "refreshed": False
                }
            }
        
    except Exception as e:
        print(f"Error retrieving tokens for channel < {channel_id} > : {str(e)}")
        return {
            "status": "error",
            "message": f"Failed to retrieve tokens for channel < {channel_id} > : {str(e)}"
        }



async def get_channel_names():
    """
    Get all secret names (channel IDs) from Google Secret Manager
    """
    try:
        client = secretmanager.SecretManagerServiceAsyncClient(credentials=credentials)
        project_id = os.getenv("GCP_PROJECT_ID")
        
        if not project_id:
            raise Exception("GCP_PROJECT_ID environment variable is not set.")
        
        parent = f"projects/{project_id}"
        
        # List all secrets in the project
        response = await client.list_secrets(request={"parent": parent})
        
        channel_names = []
        async for secret in response:
            # Extract secret name from the full path
            secret_name = secret.name.split('/')[-1]
            channel_names.append(secret_name)
        
        return channel_names
        
    except Exception as e:
        print(f"Error retrieving channel names: {str(e)}")
        raise e


@app.get("/channels")
async def get_all_channel_names():
    """
    API endpoint to get all available channel IDs (secret names)
    """
    try:
        channel_names = await get_channel_names()
        
        return {
            "status": "success",
            "data": {
                "channel_count": len(channel_names),
                "channel_ids": channel_names
            }
        }
        
    except Exception as e:
        print(f"Error retrieving channel names: {str(e)}")
        return {
            "status": "error",
            "message": f"Failed to retrieve channel names: {str(e)}"
        }



async def get_youtube_channel_details_public(channel_id: str):
    """
    Get YouTube channel public details using YouTube Data API v3 with API key
    """
    api_key = os.getenv("YOUTUBE_API_KEY")  # Add this to your .env.local
    
    if not api_key:
        raise Exception("YOUTUBE_API_KEY environment variable is not set.")
    
    url = "https://www.googleapis.com/youtube/v3/channels"
    
    params = {
        "part": "snippet,statistics",
        "id": channel_id,
        "key": api_key
    }
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as response:
                response.raise_for_status()
                data = await response.json()
                
                if not data.get("items"):
                    return None
                
                channel_info = data["items"][0]
                snippet = channel_info.get("snippet", {})
                statistics = channel_info.get("statistics", {})
                
                return {
                    "channel_id": channel_id,
                    "channel_name": snippet.get("title"),
                    "description": snippet.get("description"),
                    "profile_image_url": snippet.get("thumbnails", {}).get("high", {}).get("url"),
                    "subscriber_count": statistics.get("subscriberCount"),
                    "video_count": statistics.get("videoCount"),
                    "view_count": statistics.get("viewCount"),
                    "created_date": snippet.get("publishedAt"),
                    "country": snippet.get("country"),
                    "custom_url": snippet.get("customUrl")
                }
                
    except Exception as e:
        print(f"Error fetching channel details: {str(e)}")
        raise e


import logging

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)