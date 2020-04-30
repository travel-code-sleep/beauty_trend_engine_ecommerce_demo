from __future__ import (absolute_import, division, print_function,
                        unicode_literals)
import warnings
import os
import gc
from pathlib import Path
import time
from datetime import datetime, timedelta
from functools import reduce
from collections import Counter
import math

import matplotlib
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns
import swifter
from tqdm import tqdm
from tqdm.notebook import tqdm
from meiyume.utils import Logger, Sephora, nan_equal, show_missing_value, MeiyumeException, ModelsAlgorithms, S3FileManager, chunks

# fast ai imports
from tqdm.notebook import tqdm
from fastai import *
from fastai.text import *

# transformers imports
import torch
from transformers import pipeline
from transformers import BartForConditionalGeneration, BartTokenizer
import argparse

# text lib imports
import re
import string
import unidecode
from ast import literal_eval
import textacy
import textacy.ke as ke
from textacy import preprocessing
import pke
from nltk.corpus import stopwords
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
# spaCy based imports
import spacy
from spacy.lang.en.stop_words import STOP_WORDS
from spacy.lang.en import English
from spacy.matcher import Matcher

# multiprocessing
import multiprocessing as mp
from multiprocessing import Pool
from concurrent.futures import process

process_manager = mp.Pool(mp.cpu_count())

warnings.simplefilter(action='ignore')

file_manager = S3FileManager()

np.random.seed(1337)
tqdm.pandas()


class Ranker(ModelsAlgorithms):
    """Ranker [summary]

    [extended_summary]

    Args:
        ModelsAlgorithms ([type]): [description]

    Returns:
        [type]: [description]
    """

    def __init__(self, path='.'):
        super().__init__(path=path)

    def rank(self, meta_file=None, detail_file=None):
        """[summary]

        Keyword Arguments:
            meta_file {[file_path:str]} -- [description] (default: {None})
            detail_file {[file_path:str]} -- [description] (default: {None})

        Returns:
            [type] -- [description]
        """
        if meta_file:
            meta = pd.read_feather(meta_file)
        else:
            meta_files = self.sph.metadata_clean_path.glob(
                'cat_cleaned_sph_product_metadata_all*')
            meta = pd.read_feather(max(meta_files, key=os.path.getctime))

        dft = meta.groupby('prod_id').product_type.apply(
            ' '.join).reset_index()

        exclude_pdt = ["bath-set-gifts",
                       "beauty-tool-gifts",
                       "clean-fragrance",
                       "clean-hair-care",
                       "clean-makeup",
                       "clean-skin-care",
                       "bath-set-gifts",
                       "cologne-gift-sets",
                       "fragrance-gift-sets",
                       "fragrance-gifts-gift-value-sets-men",
                       "gifts-for-her",
                       "gifts-for-men",
                       "gifts-for-teenage-girls",
                       "gifts-for-them",
                       "gifts-under-10",
                       "gifts-under-100",
                       "gifts-under-15",
                       "gifts-under-25",
                       "gifts-under-50",
                       "gifts-under-75",
                       "hair-gift-sets",
                       "home-fragrance-candle-gift-sets",
                       "makeup-bags-accessories-by-category-gifts",
                       "makeup-gift-sets",
                       "mens-gifts",
                       "perfume-gift-sets",
                       "skin-care-gift-sets"]

        def choose_type(x):
            x = x.split()
            t = list(set(x) - set(exclude_pdt))
            if len(t) > 0:
                return t[0]
            else:
                return x[0]
        dft.product_type = dft.product_type.swifter.apply(choose_type)

        meta.drop_duplicates(subset='prod_id', inplace=True)

        dft.set_index('prod_id', inplace=True)
        meta.set_index('prod_id', inplace=True)
        meta.drop('product_type', inplace=True, axis=1)

        meta = meta.join(dft, how='left')
        meta.reset_index(inplace=True)

        if detail_file:
            detail = pd.read_feather(detail_file)
        else:
            detail_files = self.sph.detail_clean_path.glob(
                'cleaned_sph_product_detail*')
            detail = pd.read_feather(max(detail_files, key=os.path.getctime))
        detail.drop_duplicates(subset='prod_id', inplace=True)

        meta.set_index('prod_id', inplace=True)
        detail.set_index('prod_id', inplace=True)
        meta_detail = meta.join(detail, how='inner', rsuffix='detail')
        meta_detail[['rating', 'reviews', 'votes', 'would_recommend_percentage', 'five_star', 'four_star', 'three_star',
                                          'two_star', 'one_star']] = meta_detail[['rating', 'reviews', 'votes', 'would_recommend_percentage', 'five_star', 'four_star', 'three_star',
                                                                                  'two_star', 'one_star']].apply(pd.to_numeric)
        meta_detail.reset_index(inplace=True)

        review_conf = meta_detail.groupby(by=['category', 'product_type'])[
            'reviews'].mean().reset_index()
        prior_rating = meta_detail.groupby(by=['category', 'product_type'])[
            'rating'].mean().reset_index()

        meta_detail.sort_index(inplace=True)

        def total_stars(x): return x.reviews * x.rating

        def bayesian_estimate(x):
            c = round(review_conf['reviews'][(review_conf.category == x.category) & (
                review_conf.product_type == x.product_type)].values[0])
            prior = round(prior_rating['rating'][(prior_rating.category == x.category) & (
                prior_rating.product_type == x.product_type)].values[0])
            return (c * prior + x.rating * x.reviews	) / (c + x.reviews)

        meta_detail['total_stars'] = meta_detail.swifter.apply(
            lambda x: total_stars(x), axis=1).reset_index(drop=True)
        meta_detail['bayesian_estimate'] = meta_detail.swifter.apply(
            bayesian_estimate, axis=1)
        meta_detail.reset_index(drop=True, inplace=True)

        def ratio(x):
            """pass"""
            pstv_to_ngtv_stars = ((x.five_star + x.four_star)+1) / \
                ((x.two_star+1 + x.one_star+1)+1)
            pstv_to_total_stars = (x.five_star + x.four_star) / (x.reviews)
            return pstv_to_ngtv_stars, pstv_to_total_stars

        meta_detail['pstv_to_ngtv_stars'], meta_detail['pstv_to_total_stars'] = zip(*meta_detail.swifter.apply(
            lambda x: ratio(x), axis=1))

        meta_detail.meta_date = pd.to_datetime(
            meta_detail.meta_date, format='%Y-%m-%d')

        meta_detail.drop(columns=['meta_datedetail',
                                  'product_namedetail'], axis=1, inplace=True)

        columns = ["prod_id",
                   "product_name",
                   "product_page",
                   "brand",
                   "rating",
                   "category",
                   "product_type",
                   "new_flag",
                   "complete_scrape_flag",
                   "meta_date",
                   "source",
                   "low_p",
                   "high_p",
                   "mrp",
                   "clean_flag",
                   "abt_product",
                   "how_to_use",
                   "abt_brand",
                   "reviews",
                   "votes",
                   "would_recommend_percentage",
                   "five_star",
                   "four_star",
                   "three_star",
                   "two_star",
                   "one_star",
                   "total_stars",
                   "bayesian_estimate",
                   "pstv_to_ngtv_stars",
                   "pstv_to_total_stars",
                   "first_review_date"
                   ]
        meta_detail = meta_detail[columns]
        meta_detail.product_name = meta_detail.product_name.progress_apply.apply(
            unidecode.unidecode)
        meta_detail.brand = meta_detail.brand.progress_apply.apply(
            unidecode.unidecode)

        filename = f'ranked_cleaned_sph_product_meta_detail_all_{meta.meta_date.max()}'
        meta_detail.to_feather(self.output_path/filename)

        meta_detail.fillna('', inplace=True)
        meta_detail = meta_detail.replace('\n', ' ', regex=True)
        meta_detail = meta_detail.replace('~', ' ', regex=True)

        filename = filename + '.csv'
        meta_detail.to_csv(self.output_path/filename, index=None, sep='~')

        file_manager.push_file_s3(file_path=self.output_path /
                                  filename, job_name='meta_detail')
        Path(self.output_path/filename).unlink()
        return meta_detail


class SexyIngredient(ModelsAlgorithms):
    def __init__(self, path='.'):
        """[summary]

        Arguments:
            StatAlgorithm {[class]} -- [description]

        Keyword Arguments:
            path {str} -- [description] (default: {'.'})
        """
        super().__init__(path=path)

    def make(self, meta_detail_file=None, ingredient_file=None):
        """[summary]

        Arguments:
            detail_file {[type]} -- [description]

        Keyword Arguments:
            meta_file {[type]} -- [description] (default: {None})
        """
        if meta_detail_file is None:
            meta_detail_files = self.output_path.glob(
                'ranked_cleaned_sph_product_meta_detail_all*')
            filename = max(meta_detail_files, key=os.path.getctime)
        else:
            filename = meta_detail_file
        meta_rank = pd.read_feather(filename)

        if ingredient_file is None:
            ingredient_files = self.sph.detail_clean_path.glob(
                'cleaned_sph_product_ingredient_all*')
            filename = max(ingredient_files, key=os.path.getctime)
        else:
            filename = ingredient_file
        self.ingredient = pd.read_feather(filename)

        old_ing_list = self.ingredient.ingredient[self.ingredient.new_flag.str.lower() == 'old'].str.strip(
        ).tolist()

        # find new ingredients based on new products
        def find_new_ingredient(x):
            if x.new_flag.lower() == 'new':
                if x.ingredient in old_ing_list:
                    return 'New_Product'
                else:
                    return 'New_Ingredient'
            else:
                return x.new_flag
        self.ingredient.new_flag = self.ingredient.swifter.apply(
            find_new_ingredient, axis=1)

        # rule based category assignment of ingredients
        # replace withe ingredient classification model prediction/inference

        food = pd.read_excel(self.sph.detail_path /
                             'ingredient_type_db.xlsx', sheet_name='food').name.dropna().str.strip().tolist()
        chemical = pd.read_excel(self.sph.detail_path /
                                 'ingredient_type_db.xlsx', sheet_name='chemical').name.dropna().str.strip().tolist()
        organic = pd.read_excel(
            self.sph.detail_path/'ingredient_type_db.xlsx', sheet_name='organic').name.dropna().str.strip().tolist()

        def assign_food_type(x):
            if any(w in food for w in x.split()):
                return 'food'
            else:
                return np.nan

        self.ingredient['ingredient_type'] = self.ingredient.ingredient.swifter.apply(
            assign_food_type)

        def assign_organic_chemical_type(x):
            if x.ingredient_type != 'food':
                if any(w in x.ingredient for w in organic):
                    return 'natural/organic'
                elif any(wc in x.ingredient for wc in chemical):
                    return 'chemical_compound'
                else:
                    return np.nan
            else:
                return x.ingredient_type

        self.ingredient['ingredient_type'] = self.ingredient.swifter.apply(
            assign_organic_chemical_type, axis=1)

        # assign vegan type
        self.ingredient.ingredient_type[self.ingredient.ingredient.str.contains(
            'vegan')] = 'vegan'

        # add columns for ranking
        meta_rank = meta_rank[['prod_id', 'brand', 'category', 'product_name',
                               'product_type', 'rating', 'bayesian_estimate', 'low_p', 'source']]
        meta_rank.set_index('prod_id', inplace=True)
        self.ingredient.set_index('prod_id', inplace=True)

        self.ingredient = self.ingredient.join(
            meta_rank, how='left', rsuffix='_meta')
        self.ingredient.reset_index(inplace=True)

        banned_ingredients = pd.read_csv(
            self.external_path/'banned_substances.csv')
        banned_ingredients.dropna(inplace=True)
        # banned_ingredients = banned_ingredients[banned_ingredients.substances.astype(
        #     str).apply(len) > 2]
        # banned_ingredients.substances = banned_ingredients.substances.str.strip(
        # ).str.lower().astype(str)
        # banned_ingredients = banned_ingredients[banned_ingredients.substances != '-']
        # s1 = pd.Series(['paraben', 'parabens'])
        # banned_ingredients.substances = banned_ingredients.substances.append(
        #     s1).reset_index(drop=True)

        self.ingredient['ban_flag'] = self.ingredient.ingredient.swifter.apply(
            lambda x: 'Yes' if x in banned_ingredients.substances.tolist() else 'No')
        self.ingredient.clean_flag[self.ingredient.ban_flag ==
                                   'Yes'] = 'Unclean'
        columns = ["prod_id",
                   "clean_flag",
                   "ingredient",
                   "new_flag",
                   "meta_date",
                   "ingredient_type",
                   "brand",
                   "category",
                   "product_name",
                   "product_type",
                   "rating",
                   "bayesian_estimate",
                   "low_p",
                   "source",
                   "ban_flag"
                   ]
        self.ingredient = self.ingredient[columns]

        filename = str(filename).split("\\")[-1]
        self.ingredient.to_feather(
            self.output_path/f'ranked_{filename}')

        self.ingredient.fillna('', inplace=True)
        self.ingredient = self.ingredient[self.ingredient.ingredient.str.len(
        ) < 200]
        self.ingredient = self.ingredient.replace('\n', ' ', regex=True)
        self.ingredient = self.ingredient.replace('~', ' ', regex=True)

        filename = 'ranked_' + filename + '.csv'
        self.ingredient.to_csv(self.output_path/filename, index=None, sep='~')

        file_manager.push_file_s3(file_path=self.output_path /
                                  filename, job_name='ingredient')
        Path(self.output_path/filename).unlink()
        return self.ingredient


class KeyWords(ModelsAlgorithms):
    def __init__(self, path: Union[str, Path] = '.'):
        """KeyWords [summary]

        [extended_summary]

        Args:
            ModelsAlgorithms (type): parent class. instantitates the required libs and sets system paths
            path (Union[str, Path], optional): root path. Defaults to '.' (current working directory).
        """
        super().__init__(path=path)
        self.en = textacy.load_spacy_lang(
            'en_core_web_lg', disable=("parser",))
        self.analyser = SentimentIntensityAnalyzer()
        self.nlp = spacy.load("en_core_web_lg")

    def get_no_of_words(self, l):
        p = 0.2
        if l < 50:
            k = 2
        elif l >= 50 and l <= 100:
            k = 6
        elif l > 100 and l <= 300:
            k = 15
        elif l > 300 and l <= 1000:
            p = 0.18
            k = 35
        elif l > 1000 and l <= 5000:
            p = 0.16
            k = 60
        elif l > 5000:
            p = 0.14
            k = 100
        return int(round(k)), p

    def extract_keywords(self, text: Union[str, spacy.tokens.doc.Doc], include_pke: bool = False, is_doc: bool = False):
        """extract_keywords [summary]

        [extended_summary]

        Args:
            text (Union[str, spacy.tokens.doc.Doc]): [description]
            include_pke (bool, optional): [description]. Defaults to False.
            is_doc (bool, optional): [description]. Defaults to False.

        Returns:
            [type]: [description]
        """
        try:
            if is_doc:
                doc = text
                l = len(doc.text.split())
            else:
                l = len(text.split())
                doc = None
            if l > 7:
                k, p = self.get_no_of_words(l)

                if doc is None:
                    doc = textacy.make_spacy_doc(text, lang=self.en)

                if include_pke:
                    self.extractor_por = pke.unsupervised.PositionRank()
                    self.extractor_por.load_document(input=text, language='en')
                    self.extractor_por.candidate_selection()
                    self.extractor_por.candidate_weighting()

                    self.extractor_yke = pke.unsupervised.YAKE()
                    self.extractor_yke.load_document(
                        input=text, language='en')
                    stoplist = stopwords.words('english')
                    self.extractor_yke.candidate_selection(
                        n=3, stoplist=stoplist)
                    self.extractor_yke.candidate_weighting(
                        window=4, stoplist=stoplist, use_stems=False)
                    pke_keywords = [i[0] for i in self.extractor_por.get_n_best(n=k) if i[1] > 0.02] +\
                        [i[0] for i in self.extractor_yke.get_n_best(
                            n=k, threshold=0.8, redundancy_removal=True) if i[1] > 0.02]
                else:
                    pke_keywords = []

                keywords = [i[0] for i in ke.textrank(doc, window_size=4, normalize='lower', topn=k) if i[1] > 0.02] +\
                    [i[0] for i in ke.sgrank(doc, ngrams=(
                        1, 2, 3, 4, 5), normalize="lower", topn=p) if i[1] > 0.02] + pke_keywords

                self.keywords = sorted(list(set(keywords)), reverse=False,
                                       key=lambda x: len(x))

                if len(self.keywords) > 0:
                    self.bad_words = []
                    for i in range(len(self.keywords)):
                        for j in range(i + 1, len(self.keywords)):
                            if self.keywords[i] in self.keywords[j]:
                                self.bad_words.append(self.keywords[i])

                clean_keywords = [
                    word for word in self.keywords if word not in self.bad_words]

                return ', '.join(clean_keywords)  # list(set(keywords))
            else:
                return None
        except IndexError:
            return 'failed'

    def summarize_keywords(self, keywords: Union[str, list], sep: str = ',', exclude_keys: list = [], max_keys: int = -1):
        """summarize_keywords [summary]

        [extended_summary]

        Args:
            keywords (Union[str, list]): [description]
            sep (str, optional): [description]. Defaults to ','.
            exclude_keys (list, optional): [description]. Defaults to [].
            max_keys (int, optional): [description]. Defaults to -1.

        Returns:
            [type]: [description]
        """
        self.exclude_keys = ['', 'product', 'easy', 'glad', 'minutes', 'fingers', 'job', 'year',
                             'negative reviews', 'negative review', 'stuff', 'store', ]
        self.exclude_keys.extend(exclude_keys)

        if type(keywords) == list:
            keywords = Counter(keywords).items()
        else:
            keywords = Counter(keywords.split(f'{sep}')).items()

        skw = sorted([(k.strip(), v) for k, v in keywords if v >= 3 and k.strip() != ''],
                     reverse=False, key=lambda x: len(x[0]))

        self.irrelevant_keys = []
        for i in skw:
            tags = []
            tokens = self.nlp(i[0])
            for token in tokens:
                tags.append(token.pos_)
            if all(t not in ['NOUN', 'PROPN', 'PRON'] for t in tags):
                self.irrelevant_keys.append(i[0])
        skw = [item for item in skw if item[0] not in self.irrelevant_keys]

        for i in range(len(skw)):
            for j in range(i + 1, len(skw)):
                if skw[i][0] in skw[j][0]:
                    self.exclude_keys.append(skw[i][0])

        keyword_summary = {item[0]: item[1]
                           for item in skw if item[0] not in self.exclude_keys}

        if len(keyword_summary.keys()) > max_keys and max_keys != -1:
            self.keyword_summary = dict(sorted(keyword_summary.items(), key=lambda x: len(
                x[0].split()), reverse=True)[:max_keys])
        else:
            self.keyword_summary = keyword_summary

        return self.keyword_summary

    def generate_sentiment_ngrams(self, text: str, n: list = [2, 3, 4, 5, 6], min_freq: int = 2, max_terms: int = -1, exclude_ngrams: list = [],
                                  sentiment: str = 'negative', sentiment_threshold: float = 0.0, increase_threshold_by: float = 0.2):
        """generate_sentiment_ngrams [summary]

        [extended_summary]

        Args:
            text (str): [description]
            n (list, optional): [description]. Defaults to [2, 3, 4, 5, 6].
            min_freq (int, optional): [description]. Defaults to 2.
            max_terms (int, optional): [description]. Defaults to -1.
            exclude_ngrams (list, optional): [description]. Defaults to [].
            sentiment (str, optional): [description]. Defaults to 'negative'.
            sentiment_threshold (float, optional): [description]. Defaults to 0.0.
            increase_threshold_by (float, optional): [description]. Defaults to 0.2.

        Returns:
            [type]: [description]
        """
        self.exclude_ngram_list = ['good product', 'great product', 'works best', 'love love', 'great job', 'great tool', 'useful tool', 'works good', 'tool is great',
                                   'recommend this product', 'tool works great', 'like this tool', 'best way to use', 'ready to come', 'better to use', 'works pretty',
                                   'skin care', ]
        self.exclude_ngram_list.extend(exclude_ngrams)
        self.sentiment_threshold_ = sentiment_threshold

        doc = textacy.make_spacy_doc(text, lang=self.en)

        ngrams = []
        while len(ngrams) == 0:
            if min_freq == 0:
                break
            for gram in n:
                ngrams.extend(list(textacy.extract.ngrams(doc, gram, filter_stops=True, filter_punct=True,
                                                          filter_nums=True, min_freq=min_freq)))

            ngrams = [str(n) for n in ngrams]
            ngrams = [n.replace('nt ', 'dont ') for n in ngrams]

            if min_freq == 1:
                self.sentiment_threshold_ += increase_threshold_by
            if sentiment == 'negative':
                self.sentiment_threshold_ = -self.sentiment_threshold_

            if sentiment == 'positive':
                ngrams = [n for n in ngrams if self.analyser.polarity_scores(
                    n)['compound'] > self.sentiment_threshold_]
            elif sentiment == 'negative':
                ngrams = [n for n in ngrams if self.analyser.polarity_scores(
                    n)['compound'] < self.sentiment_threshold_]

            min_freq -= 1

        if len(ngrams) > 1:
            ngrams_u = list(set(ngrams))
            ngrams_u = sorted(ngrams_u, reverse=False, key=lambda x: len(x))
            for i in range(len(ngrams_u)):
                for j in range(i + 1, len(ngrams_u)):
                    if ngrams_u[i] in ngrams_u[j]:
                        self.exclude_ngram_list.append(ngrams_u[i])
            ngrams = [n for n in ngrams if n not in self.exclude_ngram_list]

            self.irrelevant_ngrams = []
            ngrams_u = list(set(ngrams))
            for i in ngrams_u:
                tags = []
                tokens = self.nlp(i)
                for token in tokens:
                    tags.append(token.pos_)
                if all(t not in ['NOUN', 'PROPN', 'PRON', 'ADJ', ] for t in tags):
                    self.irrelevant_ngrams.append(i)
            selected_grams = [
                n for n in ngrams if n not in self.irrelevant_ngrams]
            selected_grams = dict(Counter(selected_grams))

            if len(selected_grams.keys()) > max_terms and max_terms != -1:
                self.ngrams = dict(sorted(selected_grams.items(), key=lambda x: len(
                    x[0].split()), reverse=True)[:max_terms])
            else:
                self.ngrams = selected_grams
        else:
            self.ngrams = {}
        return self.ngrams


class PredictSentiment(ModelsAlgorithms):
    """PredictSentiment [summary]

    [extended_summary]

    Args:
        ModelsAlgorithms ([type]): [description]

    Returns:
        [type]: [description]
    """

    def __init__(self, model_file='sentiment_model_two_class', data_vocab_file='sentiment_class_databunch_two_class.pkl',
                 model_path=None, path='.'):
        """__init__ [summary]

        [extended_summary]

        Args:
            model_file (str, optional): [description]. Defaults to 'sentiment_model_two_class'.
            data_vocab_file (str, optional): [description]. Defaults to 'sentiment_class_databunch_two_class.pkl'.
            model_path ([type], optional): [description]. Defaults to None.
            path (str, optional): [description]. Defaults to '.'.
        """
        super().__init__(path=path)
        if model_path:
            data_class = load_data(path=model_path,
                                   file=data_vocab_file)
        else:
            data_class = load_data(path=self.model_path,
                                   file=Path(f'data/{data_vocab_file}'))

        self.learner = text_classifier_learner(
            data_class, AWD_LSTM, drop_mult=0.5)
        self.learner.load(model_file)

    def predict_instance(self, text):
        """predict_instance [summary]

        [extended_summary]

        Args:
            text ([type]): [description]

        Returns:
            [type]: [description]
        """
        pred = self.learner.predict(text)
        return pred[0], pred[1].numpy(), pred[2].numpy()

    def predict_batch(self, text_column_name, data, save=False):
        """predict_batch [summary]

        [extended_summary]

        Args:
            text_column_name ([type]): Name of the text field for which sentiment will be predicted.
            data (str:dataframe): Filename of the data file with or without complete path variable.
            save (bool, optional): True if you want to save the resulting dataframe with sentiment and probabilities. Defaults to True.

        Returns:
            [type]: [description]
        """

        if type(data) != pd.core.frame.DataFrame:
            filename = data
            try:
                data = pd.read_feather(Path(data))
            except:
                data = pd.read_csv(Path(data))
        else:
            filename = None

        data.reset_index(inplace=True, drop=True)

        self.learner.data.add_test(data[text_column_name])
        prob_preds = self.learner.get_preds(
            ds_type=DatasetType.Test, ordered=True)

        data = pd.concat([data, pd.DataFrame(
            prob_preds[0].numpy(), columns=['neg_prob', 'pos_prob'])], axis=1)

        data.neg_prob = data.neg_prob.swifter.apply(lambda x: round(x, 3))
        data.pos_prob = data.pos_prob.swifter.apply(lambda x: round(x, 3))

        data['sentiment'] = data.swifter.apply(
            lambda x: 'positive' if x.pos_prob > x.neg_prob else 'negative', axis=1)
        data.reset_index(inplace=True, drop=True)
        if filename:
            filename = str(filename).split('\\')[-1]
            if save:
                data.to_feather(
                    self.output_path/f'with_sentiment_{filename}')
        return data


class PredictInfluence(ModelsAlgorithms):

    def __init__(self):
        super().__init__()
        self.lookup_ = ['free sample', 'free test', 'complimentary test', 'complimentary review', 'complimentary review',
                        'complimentary test', 'receive product free', 'receive product complimentary', 'product complimentary',
                        'free test', 'product free', 'test purpose', 'got this as a sample']
        self.punctuations = string.punctuation
        self.stopwords = list(STOP_WORDS)
        self.parser = English()

    def spacy_tokenizer(self, text):
        """spacy_tokenizer [summary]

        [extended_summary]

        Args:
            text ([type]): [description]

        Returns:
            [type]: [description]
        """
        tokens = self.parser(text)
        tokens = [word.lemma_.lower().strip() if word.lemma_ !=
                  "-PRON-" else word.lower_ for word in tokens]
        tokens = [
            word for word in tokens if word not in self.stopwords and word not in self.punctuations]
        tokens = " ".join([i for i in tokens])
        return tokens

    def predict_instance(self, text):
        tokenized_text = self.spacy_tokenizer(text)
        if any(i in tokenized_text for i in self.lookup_):
            return 'Influenced'
        else:
            return 'Not Influenced'

    def predict_batch(self, text_column_name, data, save=False):
        """predict_batch [summary]

        [extended_summary]

        Args:
            text_column_name ([type]): [description]
            data ([type]): [description]
            save (bool, optional): [description]. Defaults to False.
        """
        if type(data) != pd.core.frame.DataFrame:
            filename = data
            try:
                data = pd.read_feather(Path(data))
            except:
                data = pd.read_csv(Path(data))
        else:
            filename = None

        data['tokenized_text'] = data[text_column_name].progress_apply(
            self.spacy_tokenizer)
        data['is_influenced'] = data.tokenized_text.progress_apply(lambda x: "Yes" if
                                                                   any(y in x for y in self.lookup_) else "No")

        data.reset_index(inplace=True, drop=True)
        if filename:
            filename = str(filename).split('\\')[-1]
            if save:
                data.to_feather(
                    self.output_path/f'{filename}')
        return data


class SelectCandidate(ModelsAlgorithms):
    """SelectCandidate [summary]

    [extended_summary]

    Args:
        ModelsAlgorithms ([type]): [description]

    Returns:
        [type]: [description]
    """

    def __init__(self):
        super().__init__()

    def select(self, data: Union[str, Path, DataFrame], weight_column: str, groupby_columns: Union[str, list], fraction: float = 0.3,
               select_column=None, sep: str = ' ', drop_weights: bool = True, keep_all: bool = True, **kwargs):
        """select [summary]

        [extended_summary]

        Args:
            data (Union[str, Path, DataFrame]): dataset. prefarably dataframe, csv or feather file.
            weight_column (str): numerical column on which weights will be calculated
            groupby_columns (Union[str, list]): columns over which values the sampling candidate groups will be generated
            fraction (float, optional): fraction of data to keep. Defaults to 0.3.
            select_column ([type], optional): the column of which rows will be combined over groups and
                                              be returned as group columns + combined data column. Defaults to None.
            sep (str, optional): separator by which the select column rows will be joined over groups. Defaults to ' '.
            drop_weights (bool, optional): whether to drop the weight column values. Defaults to True.
            keep_all (bool, optional): keep all the original groups. Defaults to True.

        Returns:
            data_sample(DataFrame): [description]
        """
        if type(groupby_columns) != list:
            groupby_columns = [groupby_columns]

        if type(data) != pd.core.frame.DataFrame:
            filename = data
            try:
                data = pd.read_feather(Path(filename))
            except:
                data = pd.read_csv(Path(filename))
        else:
            filename = None

        data[weight_column][data[weight_column] == ''] = 0
        data[weight_column] = data[weight_column].astype(int) + 1

        data['weight_avg'] = data.groupby(by=groupby_columns)[
            weight_column].transform('mean')
        data['candidate_weight'] = data[weight_column].astype(
            int)/data.weight_avg.astype(float)

        data_sample = data.groupby(by=groupby_columns, group_keys=False).apply(
            pd.DataFrame.sample, frac=fraction, weights='candidate_weight')

        if keep_all:
            missing_groups = set(set(data[groupby_columns[0]].tolist(
            ))-set(data_sample[groupby_columns[0]].tolist()))

            data_sample = pd.concat([data_sample, data[data[groupby_columns[0]].isin(
                missing_groups)]], axis=0)

        if drop_weights:
            data_sample.drop(
                ['weight_avg', 'candidate_weight'], inplace=True, axis=1)

        data_sample[weight_column] = data_sample[weight_column].astype(int) - 1

        if select_column:
            data_select = data_sample.groupby(by=groupby_columns)[
                select_column].progress_apply(f'{sep}'.join).reset_index()
            return data_select

        return data_sample


class Summarizer(ModelsAlgorithms):
    """Summarizer [summary]

    [extended_summary]

    Args:
        ModelsAlgorithms ([type]): [description]
    """

    def __init__(self, current_device: int = -1, initialize_model: bool = False):
        """__init__ [summary]

        [extended_summary]

        Args:
            current_device (int, optional): [description]. Defaults to -1.
            initialize_model (bool, optional): Set to True if using method summarize_instance or summarize_batch.
                                               Set to False if using method summarize_batch_plus. Defaults to False.
        """
        super().__init__()

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.current_device = 0 if torch.cuda.is_available() else -1

        if initialize_model:
            self.bart_summarizer = pipeline(
                task='summarization', model='bart-large-cnn', device=self.current_device)
        else:
            self.bart_summarizer = None

    def generate_summary(self, text: str, min_length=150):

        assert self.bart_summarizer is not None, "Set initialize model parameter to True when using summarize_instance or \
                                                  summarize_batch methods. "

        l = len(text.split())
        if l > 1024:
            max_length = 1024
        else:
            max_length = l

        if l < min_length+30:
            return text
        else:
            summary = self.bart_summarizer(text, min_length=min_length,
                                           max_length=max_length)
            return summary[0]['summary_text']

    def generate_summary_batch(self, examples: list, model_name: str = "bart-large-cnn", min_length: int = 150,
                               max_length: int = 1024, batch_size: int = 12):
        # device = "cuda" if torch.cuda.is_available() else "cpu"
        generated_summaries = []
        model = BartForConditionalGeneration.from_pretrained(
            model_name).to(self.device)
        tokenizer = BartTokenizer.from_pretrained("bart-large")

        for batch in tqdm(list(chunks(examples, batch_size))):
            dct = tokenizer.batch_encode_plus(
                batch, max_length=1024, return_tensors="pt", pad_to_max_length=True)

            summaries = model.generate(input_ids=dct["input_ids"].to(self.device),
                                       attention_mask=dct["attention_mask"].to(
                self.device),
                num_beams=4,
                length_penalty=2.0,
                # +2 from original because we start at step=1 and stop before max_length
                max_length=max_length + 2,
                min_length=min_length + 1,  # +1 from original because we start at step=1
                no_repeat_ngram_size=3,
                early_stopping=True,
                decoder_start_token_id=model.config.eos_token_id,
            )

            dec = [tokenizer.decode(
                g, skip_special_tokens=True, clean_up_tokenization_spaces=True) for g in summaries]

            generated_summaries.extend(dec)

        return generated_summaries

    def summarize_instance(self, text, min_length=150):
        return self.generate_summary(text, min_length=min_length)

    def summarize_batch(self, data: Union[str, Path, DataFrame], text_column_name: str, min_length=150, save=False):
        """summarize_batch [summary]

        [extended_summary]

        Args:
            data (Union[str, Path, DataFrame]): [description]
            text_column_name (str): [description]
            min_length (int, optional): [description]. Defaults to 150.
            save (bool, optional): [description]. Defaults to False.
        """
        if type(data) != pd.core.frame.DataFrame:
            filename = data
            try:
                data = pd.read_feather(Path(data))
            except:
                data = pd.read_csv(Path(data))
        else:
            filename = None

        data.reset_index(inplace=True, drop=True)

        data['summary'] = data[text_column_name].progress_apply(
            lambda x: self.generate_summary(x, min_length=min_length))

        if filename:
            filename = str(filename).split('\\')[-1]
            if save:
                data.to_feather(
                    self.output_path/f'with_summary_{filename}')

        return data

    def summarize_batch_plus(self, data: Union[str, Path, DataFrame], id_column_name: str = 'prod_id', text_column_name: str = 'text',
                             min_length: int = 150, max_length: int = 1024, batch_size: int = 12,
                             summary_column_name: str = 'summary', save=False):
        """summarize_batch_plus

        [extended_summary]

        Args:
            data (Union[str, Path, DataFrame]): [description]
            id_column_name (str, optional): [description]. Defaults to 'prod_id'.
            text_column_name (str, optional): [description]. Defaults to 'text'.
            min_length (int, optional): [description]. Defaults to 150.
            max_length (int, optional): [description]. Defaults to 1024.
            batch_size (int, optional): [description]. Defaults to 12.
            summary_column_name (str, optional): [description]. Defaults to 'summary'.
            save (bool, optional): [description]. Defaults to False.

        Returns:
            DataFrame: [description]
        """
        if type(data) != pd.core.frame.DataFrame:
            filename = data
            try:
                data = pd.read_feather(Path(data))
            except:
                data = pd.read_csv(Path(data))
        else:
            filename = None

        data.reset_index(inplace=True, drop=True)

        data['word_count'] = data[text_column_name].str.split().apply(len)

        data_to_summarize = data[data.word_count >= min_length+100]

        data_not_to_summarize = data[~data[id_column_name].isin(
            data_to_summarize[id_column_name].tolist())]

        data_to_summarize.reset_index(drop=True, inplace=True)
        data_not_to_summarize.reset_index(drop=True, inplace=True)

        summaries = self.generate_summary_batch(
            data_to_summarize[text_column_name].tolist(), min_length=min_length, max_length=max_length, batch_size=batch_size)

        data_to_summarize[summary_column_name] = pd.Series(summaries)

        data_not_to_summarize.rename(
            columns={text_column_name: summary_column_name}, inplace=True)

        self.data_summary = pd.concat([data_to_summarize[[id_column_name, summary_column_name]],
                                       data_not_to_summarize[[id_column_name, summary_column_name]]], axis=0)

        self.data_summary.reset_index(drop=True, inplace=True)

        return self.data_summary


class SexyReview(ModelsAlgorithms):
    def __init__(self, path='.'):
        super().__init__(path=path)
        self.sentiment_model = PredictSentiment(model_file='sentiment_model_two_class',
                                                data_vocab_file='sentiment_class_databunch_two_class.pkl')
        self.influence_model = PredictInfluence()
        self.keys = KeyWords()
        self.select_ = SelectCandidate()
        self.summarizer = Summarizer()

    def make(self, review_file: Union[str, Path, DataFrame], text_column_name: str = 'review_text', predict_sentiment: bool = True,
             predict_influence: bool = True, extract_keywords: bool = True):
        """make [summary]

        [extended_summary]

        Args:
            review_file (Union[str, Path, DataFrame]): [description]
            text_column_name (str, optional): [description]. Defaults to 'review_text'.
            predict_sentiment (bool, optional): [description]. Defaults to True.
            predict_influence (bool, optional): [description]. Defaults to True.
            extract_keywords (bool, optional): [description]. Defaults to True.

        Returns:
            review(DataFrame): [description]
        """
        if type(review_file) != pd.core.frame.DataFrame:
            filename = review_file
            try:
                self.review = pd.read_feather(Path(review_file))
            except:
                self.review = pd.read_csv(Path(review_file))
        else:
            filename = None
            self.review = review_file

        self.review.review_text = self.review.review_text.str.lower().str.replace(
            '…read more', '')
        self.review = self.review.replace('\n', ' ', regex=True)
        self.review.reset_index(inplace=True, drop=True)

        if predict_sentiment:
            self.review = self.sentiment_model.predict_batch(
                data=self.review, text_column_name=text_column_name, save=False)

        if predict_influence:
            self.review = self.influence_model.predict_batch(
                data=self.review, text_column_name=text_column_name, save=False)

        if extract_keywords:
            self.review['text'] = self.review.progress_apply(
                lambda x: x.review_title + ". " + x.review_text if x.review_title is not None else x.review_text, axis=1)
            self.review.text = self.review.text.str.lower().str.strip()
            self.review['keywords'] = process_manager.map(
                self.keys.extract_keywords, self.review.text)

        if filename:
            filename = str(review_file).split('\\')[-1]

            columns = ["prod_id",
                       "product_name",
                       "recommend",
                       "review_date",
                       "review_rating",
                       "review_text",
                       "review_title",
                       "meta_date",
                       "helpful_n",
                       "helpful_y",
                       "age",
                       "eye_color",
                       "hair_color",
                       "skin_tone",
                       "skin_type",
                       'neg_prob',
                       'pos_prob',
                       'sentiment',
                       'is_influenced',
                       'keywords'
                       ]
            self.review = self.review[columns]

            self.review.to_feather(
                self.output_path/f'with_keywords_sentiment_{filename}')

            self.review.fillna('', inplace=True)
            self.review = self.review.replace('\n', ' ', regex=True)
            self.review = self.review.replace('~', ' ', regex=True)

            filename = 'with_keywords_sentiment_' + filename + '.csv'
            self.review.to_csv(
                self.output_path/filename, index=None, sep='~')
            file_manager.push_file_s3(file_path=self.output_path /
                                      filename, job_name='review')
            Path(self.output_path/filename).unlink()

        return self.review

    def make_summary(self, review_file: Union[str, Path, DataFrame], summarize_review=True,  # candidate_criterion=[],
                     summarize_keywords=True, extract_ngrams=True, extract_topic=True):

        if type(review_file) != pd.core.frame.DataFrame:
            filename = review_file
            try:
                self.review = pd.read_feather(Path(review_file))
            except:
                self.review = pd.read_csv(Path(review_file))
        else:
            filename = None
            self.review = review_file

        self.review = self.review[['prod_id', 'product_name', 'review_text', 'review_title', 'helpful_n',
                                   'helpful_y', 'sentiment', 'is_influenced', 'keywords']]
        self.review = self.review.replace('\n', ' ', regex=True)
        self.review.reset_index(inplace=True, drop=True)
        self.review.fillna('', inplace=True)

        self.review['text'] = self.review.progress_apply(
            lambda x: x.review_title + ". " + x.review_text if x.review_title is not None and x.review_title != '' else x.review_text, axis=1)

        self.review.text = process_manager.map(
            preprocessing.normalize_whitespace, self.review.text.str.lower())
        self.review.keywords = self.review.keywords.str.lower()

        pos_review = self.review[self.review.sentiment == 'positive']
        neg_review = self.review[self.review.sentiment == 'negative']

        if summarize_keywords:
            pos_kw_selected = self.select_.select(data=pos_review, weight_column='helpful_y', groupby_columns=['prod_id'],
                                                  fraction=0.7, select_column='keywords', sep=', ')
            neg_kw_selected = self.select_.select(data=neg_review, weight_column='helpful_y', groupby_columns=[
                'prod_id'], fraction=0.7, select_column='keywords', sep=', ')

            pos_kw_selected['pos_keywords_summary'] = process_manager.map(
                self.keys.summarize_keywords, pos_kw_selected.keywords)

            neg_kw_selected['neg_keywords_summary'] = process_manager.map(
                self.keys.summarize_keywords, neg_kw_selected.keywords)

            pos_kw_selected.drop(columns='keywords', inplace=True)
            neg_kw_selected.drop(columns='keywords', inplace=True)

            pos_kw_selected.set_index('prod_id', inplace=True)
            neg_kw_selected.set_index('prod_id', inplace=True)

            self.keyword_summary = pos_kw_selected.join(
                neg_kw_selected, how='outer')
            self.keyword_summary.reset_index(inplace=True)

            del pos_kw_selected, neg_kw_selected
            gc.collect()

        if summarize_review or extract_topic:
            pos_review_selected = self.select_.select(data=pos_review, weight_column='helpful_y', groupby_columns=[
                'prod_id'], fraction=0.35, select_column='text', sep=' ')

            neg_review_selected = self.select_.select(data=neg_review, weight_column='helpful_y', groupby_columns=[
                'prod_id'], fraction=0.55, select_column='text', sep=' ')

        if summarize_review:
            pos_review_summary = self.summarizer.summarize_batch_plus(data=pos_review_selected, id_column_name='prod_id', text_column_name='text',
                                                                      min_length=150, max_length=1024, batch_size=10, summary_column_name='pos_summary')

            neg_review_summary = self.summarizer.summarize_batch_plus(data=neg_review_selected, id_column_name='prod_id', text_column_name='text',
                                                                      min_length=80, max_length=1024, batch_size=10, summary_column_name='neg_summary')
            pos_review_summary.set_index('prod_id', inplace=True)
            neg_review_summary.set_index('prod_id', inplace=True)

            self.review_summary = pos_review_summary.join(
                neg_review_summary, how='outer')
            self.review_summary.reset_index(inplace=True)

            del pos_review_summary, neg_review_summary
            gc.collect()

        if extract_topic:

            del pos_review_selected, neg_review_selected
            gc.collect()
            pass

        if extract_ngrams:
            df_pos_ngram = pos_review.groupby(by='prod_id')[
                'text'].progress_apply(' '.join).reset_index()

            df_neg_ngram = neg_review.groupby(by='prod_id')[
                'text'].progress_apply(' '.join).reset_index()

        return self.review_summary
