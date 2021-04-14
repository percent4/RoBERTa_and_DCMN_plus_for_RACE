# RoBERTa and DCMN+_RoBERTa for RACE

- Jclian91

I use RoBERTaMultipleChoiceModel and DCMN+ RoBERTa for RACE dataset. Recent progress see: RACE leaderboard: [http://www.qizhexie.com/data/RACE_leaderboard.html](http://www.qizhexie.com/data/RACE_leaderboard.html)

### Environment

Python 3.7, required Python modules see: `requirements.txt`

### Model train

1. Download the dataset and unzip it, put it in RACE directory;
2. For RoBERTaMultipleChoiceModel, run `run_race.sh`;
3. For DCMN+ RoBERTa, run `run_dcmn.py`.

### Model predict example


### Results
Model | RACE | RACE-M | RACE-H 
--- | --- | --- | --- |
RoBERTa_base |  |  |  
RoBERTa_large |  |  | 


### References

1. RACE Reading Comprehension Dataset: [http://www.qizhexie.com/data/RACE_leaderboard.html](http://www.qizhexie.com/data/RACE_leaderboard.html)
2. BERT-RACE on Github: [https://github.com/NoviScl/BERT-RACE](https://github.com/NoviScl/BERT-RACE)
3. DCMN+: Dual Co-Matching Network for Multi-choice Reading Comprehension: [https://arxiv.org/pdf/1908.11511.pdf](https://arxiv.org/pdf/1908.11511.pdf)




