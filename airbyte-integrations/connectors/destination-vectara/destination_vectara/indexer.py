#
# Copyright (c) 2023 Airbyte, Inc., all rights reserved.
#

import datetime
import json
import uuid
import requests

from typing import Any, Iterable, List, Mapping, Tuple, Union

from airbyte_cdk.destinations.vector_db_based.document_processor import METADATA_RECORD_ID_FIELD, METADATA_STREAM_FIELD
from airbyte_cdk.destinations.vector_db_based.indexer import Indexer
from airbyte_cdk.destinations.vector_db_based.utils import format_exception
from airbyte_cdk.models import ConfiguredAirbyteCatalog
from airbyte_cdk.models.airbyte_protocol import DestinationSyncMode
from destination_vectara.config import VectaraIndexingConfigModel
from destination_vectara.utils import is_valid_collection_name


class VectaraIndexer(Indexer):

    BASE_URL = "https://api.vectara.io/v1"

    def __init__(self, config: VectaraIndexingConfigModel):
        super().__init__(config)
        self.customer_id = config.customer_id
        self.corpus_name = config.corpus_name
        # self.corpus_id = config.corpus_id

    def check(self):
        try:
            jwt_token = self._get_jwt_token()
            if not jwt_token:
                return "Unable to get JWT Token. Confirm your Client ID and Client Secret."

            list_corpora_response = self._request(
                endpoint="list-corpora",
                data={
                    "numResults": 100, 
                    "filter": self.corpus_name
                    }
                )
            possible_corpora_ids_names_map = {corpus["id"]: corpus["name"] for corpus in list_corpora_response["corpus"] if corpus["name"] == self.corpus_name}
            if len(possible_corpora_ids_names_map) > 1:
                return f"Multiple Corpora exist with name {self.corpus_name}"
            if len(possible_corpora_ids_names_map) == 1:
                self.corpus_id = list(possible_corpora_ids_names_map.keys())[0]
            else:
                create_corpus_response = self._request(
                    endpoint="create-corpus",
                    data={
                        "corpus": {
                            "name": self.corpus_name,
                            "filterAttributes": [
                                    {
                                        "name": METADATA_STREAM_FIELD,
                                        "indexed": True,
                                        "type": "FILTER_ATTRIBUTE_TYPE__TEXT",
                                        "level": "FILTER_ATTRIBUTE_LEVEL__DOCUMENT"
                                    },
                                    {
                                        "name": METADATA_RECORD_ID_FIELD,
                                        "indexed": True,
                                        "type": "FILTER_ATTRIBUTE_TYPE__TEXT",
                                        "level": "FILTER_ATTRIBUTE_LEVEL__DOCUMENT"
                                    }
                                ]
                            }
                        }
                    )
                self.corpus_id = create_corpus_response["corpusId"]

        except Exception as e:
            return format_exception(e)
        
    def pre_sync(self, catalog: ConfiguredAirbyteCatalog) -> None:
        streams_to_overwrite = [
            stream.stream.name for stream in catalog.streams if stream.destination_sync_mode == DestinationSyncMode.overwrite
        ]
        if len(streams_to_overwrite):
            self._delete_doc_by_metadata(field_name=METADATA_STREAM_FIELD, field_values=streams_to_overwrite)

    def delete(self, delete_ids, namespace, stream):
        if len(delete_ids) > 0:
            self._delete_doc_by_metadata(field_name=METADATA_RECORD_ID_FIELD, field_values=delete_ids)

    def index(self, document_chunks, namespace, stream):
        for chunk in document_chunks:
            self._index_document(chunk=chunk)

    def _request(
        self, endpoint: str, http_method: str = "POST", params: Mapping[str, Any] = None, data: Mapping[str, Any] = None
    ) -> requests.Response:
        
        url = f"{self.BASE_URL}/{endpoint}"

        current_ts = datetime.datetime.now().timestamp()
        if self.jwt_token_expires_ts - current_ts <= 60:
            self._get_jwt_token()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json", 
            "Authorization": f"Bearer {self.jwt_token}",
            "customer-id": self.customer_id
            }

        response = requests.request(method=http_method, url=url, headers=headers, params=params, data=json.dumps(data))
        response.raise_for_status()
        return response.json()

    def _get_jwt_token(self):
        """Connect to the server and get a JWT token."""
        token_endpoint = f"https://vectara-prod-{self.config.customer_id}.auth.us-west-2.amazoncognito.com/oauth2/token"
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            }
        data = {
            "grant_type": "client_credentials",
            "client_id": self.config.oauth2.client_id, #4ppikkvkl55a0ofrqijr33ord4
            "client_secret": self.config.oauth2.client_secret #1olevsovotp2s912utj5av3stgbe0m72n201fku2pumf9e5hm49j
        }

        request_time = datetime.datetime.now().timestamp()
        response = requests.request(method="POST", url=token_endpoint, headers=headers, data=data)
        response_json = response.json()

        token = response_json["access_token"]
        expires_in = request_time + response_json["expires_in"]

        self.jwt_token = token
        self.jwt_token_expires_ts = expires_in
        return token
    
    def _delete_doc_by_metadata(self, metadata_field, metadata_field_value):
        query_documents_response = self._request(
            endpoint="query",
            data= {
                "query": [
                        {
                            "query": "", 
                            "numResults": 100,
                            "corpusKey": [
                                {
                                "customerId": self.customer_id,
                                "corpusId": self.corpus_id,
                                "metadataFilter": f"doc.{metadata_field} = '{metadata_field_value}'"
                                }
                            ]
                        }
                    ]
                }
            )
        document_ids = [document["id"] for document in query_documents_response["responseSet"]["document"]]
        documents_not_deleted = []
        for document_id in document_ids:
            delete_document_response = self._request(
                endpoint="delete-doc",
                data={
                    "customerId": self.customer_id, 
                    "corpusId": self.corpus_id,
                    "documentId": document_id
                    }
                )
            if delete_document_response:
                documents_not_deleted.append(document_id)
        return documents_not_deleted

    def _index_document(self, chunk):
        document_embedding = chunk.embedding  # TODO Where to put the document embeddings?
        document_metadata = self._normalize(chunk.metadata)
        index_document_response = self._request(
            endpoint="index",
            data={
                    "customerId": self.customer_id, 
                    "corpusId": self.corpus_id,
                    "document": {
                        {
                            "documentId": uuid.uuid4().int,
                            "metadataJson": json.dumps(document_metadata),
                            "section": [
                                {
                                    "title": "Content",
                                    "text": chunk.page_content,
                                }
                            ]
                        }
                    }
                }
            )
        assert index_document_response["status"]["code"] == "OK", index_document_response["status"]["statusDetail"]
    
    def _normalize(self, metadata: dict) -> dict:
        result = {}
        for key, value in metadata.items():
            if isinstance(value, (str, int, float, bool)):
                result[key] = value
            else:
                # JSON encode all other types
                result[key] = json.dumps(value)
        return result
