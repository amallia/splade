import gzip
import json
import os
import pickle
import random
import zstandard as zstd
import io

from torch.utils.data import Dataset
from tqdm.auto import tqdm
from tqdm.auto import tqdm


class PairsDatasetPreLoad(Dataset):
    """
    dataset to iterate over a collection of pairs, format per line: q \t d_pos \t d_neg
    we preload everything in memory at init
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.id_style = "row_id"

        self.data_dict = {}  # => dict that maps the id to the line offset (position of pointer in the file)
        print("Preloading dataset")
        self.data_dir = os.path.join(self.data_dir, "raw.tsv")
        with open(self.data_dir) as reader:
            for i, line in enumerate(tqdm(reader)):
                if len(line) > 1:
                    query, pos, neg = line.split("\t")  # first column is id
                    self.data_dict[i] = (query.strip(), pos.strip(), neg.strip())
        self.nb_ex = len(self.data_dict)

    def __len__(self):
        return self.nb_ex

    def __getitem__(self, idx):
        return self.data_dict[idx]


class DistilPairsDatasetPreLoad(Dataset):
    """
    dataset to iterate over a collection of pairs, format per line: q \t d_pos \t d_neg \t s_pos \t s_neg
    we preload everything in memory at init
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.id_style = "row_id"
        self.data_dict = {}  # => dict that maps the id to the line offset (position of pointer in the file)
        print("Preloading dataset")
        self.data_dir = os.path.join(self.data_dir, "raw.tsv")
        with open(self.data_dir) as reader:
            for i, line in enumerate(tqdm(reader)):
                if len(line) > 1:
                    q, d_pos, d_neg, s_pos, s_neg = line.split("\t")
                    self.data_dict[i] = (
                        q.strip(), d_pos.strip(), d_neg.strip(), float(s_pos.strip()), float(s_neg.strip()))
        self.nb_ex = len(self.data_dict)

    def __len__(self):
        return self.nb_ex

    def __getitem__(self, idx):
        return self.data_dict[idx]


class CollectionDatasetPreLoad(Dataset):
    """
    dataset to iterate over a document/query collection, format per line: format per line: doc_id \t doc
    we preload everything in memory at init
    """

    def __init__(self, data_dir):
        self.data_dir = data_dir
        self.data_dir = data_dir
        self.files = [os.path.join(data_dir, f) for f in os.listdir(data_dir) if f.endswith('.json.zstd')]
        self.files.sort()  # Ensure consistent order

        self.index = self._build_index()

    def _build_index(self):
        # This function builds an index to quickly find which file and line number a given index corresponds to
        index = []
        line_count = 0
        for file_path in tqdm(self.files):
            with open(file_path, 'rb') as fh:
                dctx = zstd.ZstdDecompressor()
                with dctx.stream_reader(fh) as reader:
                    text_stream = io.TextIOWrapper(reader, encoding='utf-8')
                    file_lines = sum(1 for _ in text_stream)
                    index.append((file_path, line_count, line_count + file_lines))
                    line_count += file_lines
        return index

    def __len__(self):
        # Returns the total number of lines across all files
        if self.index:
            _, _, total_lines = self.index[-1]
            return total_lines
        return 0


    def __getitem__(self, idx):
        # Finds the correct file and line number for the given index
        for file_path, start_idx, end_idx in self.index:
            if start_idx <= idx < end_idx:
                line_number = idx - start_idx
                with open(file_path, 'rb') as fh:
                    dctx = zstd.ZstdDecompressor()
                    with dctx.stream_reader(fh) as reader:
                        text_stream = io.TextIOWrapper(reader, encoding='utf-8')
                        for _ in range(line_number):
                            next(text_stream)  # Skip lines until the desired one
                        line = next(text_stream)
                        js_obj = json.loads(line)  # Assuming each line is a valid JSON object
                        return js_obj['id'], js_obj['text']
        raise IndexError("Index out of bounds")


class BeirDataset(Dataset):
    """
    dataset to iterate over a BEIR collection
    we preload everything in memory at init
    """

    def __init__(self, value_dictionary, information_type="document"):
        assert information_type in ["document", "query"]
        self.value_dictionary = value_dictionary
        self.information_type = information_type
        if self.information_type == "document":
            self.value_dictionary = dict()
            for key, value in value_dictionary.items():
                self.value_dictionary[key] = value["title"] + " " + value["text"]
        self.idx_to_key = {idx: key for idx, key in enumerate(self.value_dictionary)}

    def __len__(self):
        return len(self.value_dictionary)

    def __getitem__(self, idx):
        true_idx = self.idx_to_key[idx]
        return idx, self.value_dictionary[true_idx]


class MsMarcoHardNegatives(Dataset):
    """
    class used to work with the hard-negatives dataset from sentence transformers
    see: https://huggingface.co/datasets/sentence-transformers/msmarco-hard-negatives
    """

    def __init__(self, dataset_path, document_dir, query_dir, qrels_path):
        self.document_dataset = CollectionDatasetPreLoad(document_dir, id_style="content_id")
        self.query_dataset = CollectionDatasetPreLoad(query_dir, id_style="content_id")
        with gzip.open(dataset_path, "rb") as fIn:
            self.scores_dict = pickle.load(fIn)
        query_list = list(self.scores_dict.keys())
        with open(qrels_path) as reader:
            self.qrels = json.load(reader)
        self.query_list = list()
        for query in query_list:
            if str(query) in self.qrels.keys():
                self.query_list.append(query)
        print("QUERY SIZE = ", len(self.query_list))

    def __len__(self):
        return len(self.query_list)

    def __getitem__(self, idx):
        query = self.query_list[idx]
        q = self.query_dataset[str(query)][1]
        candidates_dict = self.scores_dict[query]
        candidates = list(candidates_dict.keys())
        positives = list(self.qrels[str(query)].keys())
        for positive in positives:
            candidates.remove(int(positive))
        positive = random.sample(positives, 1)[0]
        s_pos = candidates_dict[int(positive)]
        negative = random.sample(candidates, 1)[0]
        s_neg = candidates_dict[negative]
        d_pos = self.document_dataset[positive][1]
        d_neg = self.document_dataset[str(negative)][1]
        return q.strip(), d_pos.strip(), d_neg.strip(), float(s_pos), float(s_neg)


class IR_Dataset(Dataset):
    """
    dataset to iterate over a document/query collection, receives a dictionary id, text
    """

    def __init__(self, ir_dataset, information_type="document", sequential_idx=True, all_docs=None):
        assert information_type in ["document", "query"]
        self.ir_dataset = ir_dataset
        
        self.information_type = information_type
        self.value_dictionary = dict()
        self.key_to_id = dict()
        self.sequential_idx = sequential_idx
        self.all_docs = all_docs

        # We use the natural order of query/document as key, because ids of the different beir datasets
        # have different types (str vs int) which is problematic with FAISS index.
        if self.information_type == "document":
            for idx, value in enumerate(tqdm(ir_dataset.docs_iter())):
                if not sequential_idx:
                    idx = value.doc_id
                    if all_docs:
                        if idx not in all_docs:
                            continue
                    idx = value.doc_id.replace('"',"")

                try:
                    self.value_dictionary[idx] = value.title+" "+value.text
                except:
                    try:
                        self.value_dictionary[idx] = value.body.decode('iso-8859-1')+" "+value.url
                    except:
                        self.value_dictionary[idx] = value.text
                self.key_to_id[idx] = value.doc_id
        if self.information_type == "query":
            for idx, value in enumerate(ir_dataset.queries_iter()):
                if not sequential_idx:
                    idx = value.query_id
                self.value_dictionary[idx] = value.text
                self.key_to_id[idx] = value.query_id

    def __len__(self):
        return len(self.value_dictionary)

    def __getitem__(self, idx):
        return idx, self.value_dictionary[idx]
    
class IR_Dataset_NoLoad(Dataset):
    """
    dataset to iterate over a document/query collection, receives a dictionary id, text
    """

    def __init__(self, ir_dataset):
        self.ir_dataset = ir_dataset
        self.docs_store = ir_dataset.docs_store()
        all_decoded = dict()

    def __len__(self):
        return len(self.value_dictionary)

    def __getitem__(self, idx):
        value = self.docs_store.get(idx)
        text = ""
        try: 
            text = value.title+" "+value.text
        except:
            try:
                text = value.body.decode('iso-8859-1')+" "+value.url
            except:
                text = value.text
        return idx, text
